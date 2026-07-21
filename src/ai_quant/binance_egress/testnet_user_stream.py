"""Fail-closed Binance Testnet user-data stream observation and evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ai_quant.binance_egress.testnet_probe import (
    TESTNET_STREAM_HOST,
    TestnetProbeError,
)
from ai_quant.binance_egress.testnet_stream import (
    masked_websocket_frame,
    open_testnet_stream_websocket,
    read_websocket_frame,
)

ALLOWED_USER_EVENT_TYPES = frozenset(
    {"ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE", "ALGO_UPDATE", "listenKeyExpired"}
)
LISTEN_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,256}$")


class ListenKeyClient(Protocol):
    def create_listen_key(self) -> str: ...

    def keepalive_listen_key(self, listen_key: str) -> None: ...

    def close_listen_key(self, listen_key: str) -> None: ...


class UserDataEventJournal:
    """Append hash-chained, deduplicated, secret-free user event evidence."""

    def __init__(self, evidence_file: Path, state_file: Path) -> None:
        self.evidence_file = evidence_file
        self.state_file = state_file
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._last_record_sha256 = "0" * 64
        self._loaded_state = False
        self._state: dict[str, Any] = {
            "schema_version": "1.0.0",
            "status": "STARTING",
            "environment": "testnet",
            "stream_authority": "BINANCE_TESTNET_FSTREAM",
            "destination_host": TESTNET_STREAM_HOST,
            "production_endpoint_requests": 0,
            "connection_attempt_count": 0,
            "connection_count": 0,
            "reconnect_count": 0,
            "keepalive_count": 0,
            "accepted_event_count": 0,
            "duplicate_event_count": 0,
            "invalid_event_count": 0,
            "event_type_counts": {},
            "last_event_at": None,
            "last_connected_at": None,
            "last_keepalive_at": None,
            "last_error_type": None,
            "last_record_sha256": self._last_record_sha256,
            "updated_at": _utc_now(),
        }
        self._load_state()
        self._load_existing()
        self._write_state()

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        if self.state_file.is_symlink() or not self.state_file.is_file():
            raise ValueError("user-data state path is unsafe")
        try:
            loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("user-data state JSON is invalid") from exc
        if not isinstance(loaded, dict) or set(loaded) != set(self._state):
            raise ValueError("user-data state document is not closed")
        if (
            loaded.get("schema_version") != "1.0.0"
            or loaded.get("environment") != "testnet"
            or loaded.get("destination_host") != TESTNET_STREAM_HOST
            or loaded.get("production_endpoint_requests") != 0
            or not isinstance(loaded.get("event_type_counts"), dict)
        ):
            raise ValueError("user-data state boundary is invalid")
        counter_names = {
            "connection_attempt_count",
            "connection_count",
            "reconnect_count",
            "keepalive_count",
            "accepted_event_count",
            "duplicate_event_count",
            "invalid_event_count",
        }
        if (
            loaded.get("status") not in {"STARTING", "RUNNING", "STOPPED"}
            or any(
                not isinstance(loaded.get(name), int) or int(loaded[name]) < 0
                for name in counter_names
            )
            or int(loaded["connection_count"]) > int(loaded["connection_attempt_count"])
            or int(loaded["reconnect_count"]) > int(loaded["connection_attempt_count"])
            or not re.fullmatch(r"[0-9a-f]{64}", str(loaded.get("last_record_sha256")))
        ):
            raise ValueError("user-data state counters are invalid")
        self._state.update(loaded)
        self._loaded_state = True

    def _load_existing(self) -> None:
        if not self.evidence_file.exists():
            return
        summary, seen = _verify_user_data_evidence(self.evidence_file)
        if self._loaded_state and (
            self._state["accepted_event_count"] != summary["record_count"]
            or self._state["event_type_counts"] != summary["event_type_counts"]
            or self._state["last_record_sha256"] != summary["final_record_sha256"]
        ):
            raise ValueError("user-data state and journal mismatch")
        self._seen = seen
        self._last_record_sha256 = str(summary["final_record_sha256"])
        self._state["last_record_sha256"] = self._last_record_sha256
        if not self._loaded_state:
            self._state["accepted_event_count"] = summary["record_count"]
            self._state["event_type_counts"] = summary["event_type_counts"]

    def mark_running(self) -> None:
        self._update(status="RUNNING")

    def mark_stopped(self) -> None:
        self._update(status="STOPPED")

    def note_connection_attempt(self) -> None:
        with self._lock:
            reconnect = int(self._state["connection_attempt_count"]) > 0
            self._state["connection_attempt_count"] = (
                int(self._state["connection_attempt_count"]) + 1
            )
            if reconnect:
                self._state["reconnect_count"] = int(self._state["reconnect_count"]) + 1
            self._write_state_locked()

    def note_connected(self) -> None:
        with self._lock:
            self._state["connection_count"] = int(self._state["connection_count"]) + 1
            self._state["last_connected_at"] = _utc_now()
            self._state["last_error_type"] = None
            self._write_state_locked()

    def note_keepalive(self) -> None:
        with self._lock:
            self._state["keepalive_count"] = int(self._state["keepalive_count"]) + 1
            self._state["last_keepalive_at"] = _utc_now()
            self._write_state_locked()

    def note_error(self, error: BaseException) -> None:
        self._update(last_error_type=type(error).__name__)

    def record_event(self, document: Mapping[str, Any], *, received_at: str | None = None) -> bool:
        event_type = document.get("e")
        event_time = document.get("E")
        if event_type not in ALLOWED_USER_EVENT_TYPES or not isinstance(event_time, int):
            with self._lock:
                self._state["invalid_event_count"] = int(self._state["invalid_event_count"]) + 1
                self._write_state_locked()
            return False
        sanitized = _sanitize_event(document)
        stable_event_id = hashlib.sha256(_canonical(sanitized)).hexdigest()
        with self._lock:
            if stable_event_id in self._seen:
                self._state["duplicate_event_count"] = int(self._state["duplicate_event_count"]) + 1
                self._write_state_locked()
                return False
            received = received_at or _utc_now()
            content = {
                "schema_version": "1.0.0",
                "record_type": "TESTNET_USER_DATA_EVENT",
                "raw_event_type": event_type,
                "stable_event_id": stable_event_id,
                "event_time_ms": event_time,
                "received_at": received,
                "payload": sanitized,
                "environment": "testnet",
                "production_endpoint_requests": 0,
            }
            self._append_locked(content)
            self._seen.add(stable_event_id)
            self._state["accepted_event_count"] = int(self._state["accepted_event_count"]) + 1
            counts = self._state["event_type_counts"]
            if not isinstance(counts, dict):
                raise RuntimeError("user-data event counters are invalid")
            counts[event_type] = int(counts.get(event_type, 0)) + 1
            self._state["last_event_at"] = received
            self._write_state_locked()
            return True

    def _append_locked(self, content: dict[str, Any]) -> None:
        self.evidence_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.evidence_file.exists() and self.evidence_file.is_symlink():
            raise ValueError("user-data evidence path is unsafe")
        material = {"content": content, "previous_sha256": self._last_record_sha256}
        record_sha256 = hashlib.sha256(_canonical(material)).hexdigest()
        record = {**material, "record_sha256": record_sha256}
        descriptor = os.open(
            self.evidence_file,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "ab") as handle:
            handle.write(_canonical(record) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._last_record_sha256 = record_sha256
        self._state["last_record_sha256"] = record_sha256

    def _update(self, **values: object) -> None:
        with self._lock:
            self._state.update(values)
            self._write_state_locked()

    def _write_state(self) -> None:
        with self._lock:
            self._write_state_locked()

    def _write_state_locked(self) -> None:
        self._state["updated_at"] = _utc_now()
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state_file.with_name(f".{self.state_file.name}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical(self._state) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_file)


class TestnetUserDataStream:
    """Maintain a Testnet listen key, reconnect, and journal supported events."""

    def __init__(
        self,
        client: ListenKeyClient,
        journal: UserDataEventJournal,
        *,
        keepalive_interval_seconds: int = 1_800,
        rotate_no_later_than_seconds: int = 84_600,
        reconnect_delay_seconds: float = 1.0,
        websocket_opener: Callable[[str, str], AbstractContextManager[Any]] = (
            open_testnet_stream_websocket
        ),
        frame_reader: Callable[[Any], tuple[int, bytes]] = read_websocket_frame,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 30 <= keepalive_interval_seconds < rotate_no_later_than_seconds:
            raise ValueError("user-data keepalive configuration is invalid")
        if not 300 <= rotate_no_later_than_seconds <= 84_600:
            raise ValueError("user-data rotation threshold is invalid")
        if not 0.1 <= reconnect_delay_seconds <= 60:
            raise ValueError("user-data reconnect delay is invalid")
        self.client = client
        self.journal = journal
        self.keepalive_interval_seconds = keepalive_interval_seconds
        self.rotate_no_later_than_seconds = rotate_no_later_than_seconds
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.websocket_opener = websocket_opener
        self.frame_reader = frame_reader
        self.monotonic = monotonic
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> int:
        self.journal.mark_running()
        try:
            while not self._stop.is_set():
                self.journal.note_connection_attempt()
                listen_key: str | None = None
                try:
                    listen_key = self.client.create_listen_key()
                    if LISTEN_KEY_PATTERN.fullmatch(listen_key) is None:
                        raise TestnetProbeError("LISTEN_KEY_INVALID")
                    self._consume_connection(listen_key)
                except (OSError, TimeoutError, ValueError, TestnetProbeError) as exc:
                    self.journal.note_error(exc)
                finally:
                    if listen_key is not None:
                        try:
                            self.client.close_listen_key(listen_key)
                        except (OSError, TimeoutError, ValueError, TestnetProbeError) as exc:
                            self.journal.note_error(exc)
                if not self._stop.is_set():
                    self._stop.wait(self.reconnect_delay_seconds)
        finally:
            self.journal.mark_stopped()
        return 0

    def _consume_connection(self, listen_key: str) -> None:
        path = f"/private/ws/{listen_key}"
        started = self.monotonic()
        next_keepalive = started + self.keepalive_interval_seconds
        with self.websocket_opener(TESTNET_STREAM_HOST, path) as connection:
            connection.settimeout(5)
            self.journal.note_connected()
            while not self._stop.is_set():
                now = self.monotonic()
                if now - started >= self.rotate_no_later_than_seconds:
                    return
                if now >= next_keepalive:
                    self.client.keepalive_listen_key(listen_key)
                    self.journal.note_keepalive()
                    next_keepalive = now + self.keepalive_interval_seconds
                try:
                    opcode, payload = self.frame_reader(connection)
                except TimeoutError:
                    continue
                if opcode == 0x8:
                    return
                if opcode == 0x9:
                    connection.sendall(masked_websocket_frame(0xA, payload))
                    continue
                if opcode != 0x1:
                    continue
                try:
                    decoded = json.loads(payload)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.journal.record_event({})
                    continue
                if not isinstance(decoded, dict):
                    self.journal.record_event({})
                    continue
                document = decoded.get("data", decoded)
                if not isinstance(document, dict):
                    self.journal.record_event({})
                    continue
                self.journal.record_event(document)
                if document.get("e") == "listenKeyExpired":
                    return


def _sanitize_event(value: Mapping[str, Any]) -> dict[str, Any]:
    event_type = value.get("e")
    if event_type == "ACCOUNT_UPDATE":
        account = value.get("a")
        sanitized_account: dict[str, Any] = {}
        if isinstance(account, Mapping):
            sanitized_account["m"] = account.get("m")
            positions = account.get("P")
            if isinstance(positions, list):
                sanitized_account["P"] = _sanitize_value(positions)
        base = {key: item for key, item in value.items() if key != "a"}
        base["a"] = sanitized_account
        sanitized_account_event = _sanitize_value(base)
        if not isinstance(sanitized_account_event, dict):
            raise TypeError("sanitized account event is invalid")
        return sanitized_account_event
    sanitized = _sanitize_value(dict(value))
    if not isinstance(sanitized, dict):
        raise TypeError("sanitized user-data event is invalid")
    return sanitized


def verify_user_data_evidence(path: Path) -> dict[str, Any]:
    """Verify the complete closed journal and return a deterministic summary."""
    summary, _seen = _verify_user_data_evidence(path)
    return summary


def _verify_user_data_evidence(path: Path) -> tuple[dict[str, Any], set[str]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("user-data evidence path is unsafe")
    previous = "0" * 64
    seen: set[str] = set()
    counts: dict[str, int] = {}
    records = 0
    expected_content_keys = {
        "schema_version",
        "record_type",
        "raw_event_type",
        "stable_event_id",
        "event_time_ms",
        "received_at",
        "payload",
        "environment",
        "production_endpoint_requests",
    }
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid user-data evidence JSON at line {number}") from exc
        if not isinstance(record, dict) or set(record) != {
            "content",
            "previous_sha256",
            "record_sha256",
        }:
            raise ValueError(f"user-data evidence record {number} is not closed")
        material = {
            "content": record["content"],
            "previous_sha256": record["previous_sha256"],
        }
        expected_record_hash = hashlib.sha256(_canonical(material)).hexdigest()
        if record["previous_sha256"] != previous or record["record_sha256"] != expected_record_hash:
            raise ValueError(f"user-data evidence hash mismatch at line {number}")
        content = record["content"]
        if not isinstance(content, dict) or set(content) != expected_content_keys:
            raise ValueError(f"user-data evidence content {number} is not closed")
        event_type = content["raw_event_type"]
        payload = content["payload"]
        stable_event_id = content["stable_event_id"]
        if (
            content["schema_version"] != "1.0.0"
            or content["record_type"] != "TESTNET_USER_DATA_EVENT"
            or event_type not in ALLOWED_USER_EVENT_TYPES
            or not isinstance(content["event_time_ms"], int)
            or not isinstance(content["received_at"], str)
            or not isinstance(payload, dict)
            or content["environment"] != "testnet"
            or content["production_endpoint_requests"] != 0
            or not isinstance(stable_event_id, str)
            or stable_event_id != hashlib.sha256(_canonical(payload)).hexdigest()
        ):
            raise ValueError(f"user-data evidence semantics invalid at line {number}")
        if stable_event_id in seen:
            raise ValueError(f"duplicate stable user-data event at line {number}")
        seen.add(stable_event_id)
        counts[str(event_type)] = counts.get(str(event_type), 0) + 1
        records += 1
        previous = expected_record_hash
    return (
        {
            "schema_version": "1.0.0",
            "result": "PASS",
            "environment": "testnet",
            "record_count": records,
            "event_type_counts": dict(sorted(counts.items())),
            "duplicate_stable_event_count": 0,
            "production_endpoint_requests": 0,
            "final_record_sha256": previous,
        },
        seen,
    )


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            normalized = re.sub(r"[^a-z0-9]", "", name.lower())
            if normalized in {
                "listenkey",
                "apikey",
                "secret",
                "secretkey",
                "signature",
                "token",
                "password",
                "privatekey",
            }:
                continue
            result[name] = _sanitize_value(item)
        return result
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
