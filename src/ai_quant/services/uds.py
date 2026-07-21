"""Bounded one-request Unix-domain-socket framing for security-critical IPC."""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_quant.rate_budget.authorization import PeerCredentials, peer_credentials

RATE_FRAME_MAX_BYTES = 1_048_576
UDS_FRAME_HARD_MAX_BYTES = 16_777_216
FRAME_HEADER_BYTES = 4
NOTIFY_COMMITTED_ACK = b"\x06"


class UdsProtocolError(ValueError):
    """A local IPC peer sent an invalid or over-limit frame."""


@dataclass(frozen=True, slots=True)
class UnixSocketPeerExpectation:
    inode: int
    owner_uid: int
    owner_gid: int
    peer_uid: int
    peer_gid: int
    mode: int = 0o660


@dataclass(frozen=True, slots=True)
class UnixSocketServerExpectation:
    runtime_directory_gid: int
    socket_owner_uid: int
    socket_owner_gid: int


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UdsProtocolError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def encode_frame(document: Mapping[str, Any], *, max_bytes: int = RATE_FRAME_MAX_BYTES) -> bytes:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if not payload or len(payload) > max_bytes:
        raise UdsProtocolError("FRAME_SIZE_INVALID")
    return struct.pack("!I", len(payload)) + payload


def _receive_exact(peer: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = peer.recv(remaining)
        if not chunk:
            raise UdsProtocolError("TRUNCATED_FRAME")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def receive_frame(
    peer: socket.socket,
    *,
    max_bytes: int = RATE_FRAME_MAX_BYTES,
) -> Mapping[str, Any]:
    header = _receive_exact(peer, FRAME_HEADER_BYTES)
    (length,) = struct.unpack("!I", header)
    if length < 2 or length > max_bytes:
        raise UdsProtocolError("FRAME_SIZE_INVALID")
    payload = _receive_exact(peer, length)
    try:
        decoded = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UdsProtocolError("FRAME_JSON_INVALID") from exc
    if not isinstance(decoded, dict):
        raise UdsProtocolError("FRAME_DOCUMENT_NOT_OBJECT")
    return decoded


class BoundedUnixServer:
    """Single-process bounded server; each connection carries exactly one request."""

    def __init__(
        self,
        path: Path,
        handler: Callable[[Mapping[str, Any], PeerCredentials], Mapping[str, Any] | None],
        *,
        identity_expectation: UnixSocketServerExpectation,
        max_frame_bytes: int = RATE_FRAME_MAX_BYTES,
        backlog: int = 128,
        peer_timeout_seconds: float = 1.0,
        accept_timeout_seconds: float | None = None,
    ) -> None:
        if max_frame_bytes < 256 or max_frame_bytes > UDS_FRAME_HARD_MAX_BYTES:
            raise UdsProtocolError("SERVER_FRAME_LIMIT_INVALID")
        if backlog < 1 or backlog > 128:
            raise UdsProtocolError("SERVER_BACKLOG_INVALID")
        if peer_timeout_seconds <= 0 or peer_timeout_seconds > 5:
            raise UdsProtocolError("SERVER_TIMEOUT_INVALID")
        if accept_timeout_seconds is not None and not 0 < accept_timeout_seconds <= 30:
            raise UdsProtocolError("SERVER_TIMEOUT_INVALID")
        self._path = path
        self._handler = handler
        self._identity_expectation = identity_expectation
        self._max_frame_bytes = max_frame_bytes
        self._backlog = backlog
        self._peer_timeout_seconds = peer_timeout_seconds
        self._accept_timeout_seconds = accept_timeout_seconds
        self._listener: socket.socket | None = None

    def start(self) -> None:
        if self._listener is not None:
            raise UdsProtocolError("SERVER_ALREADY_STARTED")
        parent = self._path.parent
        metadata = parent.stat()
        parent_mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != self._identity_expectation.runtime_directory_gid
            or parent_mode & 0o007
            or parent_mode & 0o070 != 0o070
            or not metadata.st_mode & stat.S_ISGID
        ):
            raise UdsProtocolError("RUNTIME_DIRECTORY_UNSAFE")
        if self._path.exists() or self._path.is_symlink():
            raise UdsProtocolError("SOCKET_PATH_ALREADY_EXISTS")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self._path))
            # Frozen UDS contract requires group read/write and forbids world access.
            os.chmod(self._path, 0o660)  # nosec B103
            socket_metadata = self._path.stat()
            if (
                socket_metadata.st_uid != self._identity_expectation.socket_owner_uid
                or socket_metadata.st_gid != self._identity_expectation.socket_owner_gid
                or stat.S_IMODE(socket_metadata.st_mode) != 0o660
            ):
                raise UdsProtocolError("SERVER_SOCKET_IDENTITY_MISMATCH")
            listener.listen(self._backlog)
            listener.settimeout(self._accept_timeout_seconds)
        except BaseException:
            listener.close()
            self._path.unlink(missing_ok=True)
            raise
        self._listener = listener

    def serve_one(self) -> None:
        if self._listener is None:
            raise UdsProtocolError("SERVER_NOT_STARTED")
        peer, _ = self._listener.accept()
        try:
            peer.settimeout(self._peer_timeout_seconds)
            credentials = peer_credentials(peer)
            request = receive_frame(peer, max_bytes=self._max_frame_bytes)
            response = self._handler(request, credentials)
            if response is not None:
                peer.sendall(encode_frame(response, max_bytes=self._max_frame_bytes))
            else:
                # EOF alone is ambiguous: it is also produced when the handler raises.
                # A transport-level ACK is emitted only after the durable handler returns.
                peer.sendall(NOTIFY_COMMITTED_ACK)
        finally:
            peer.close()

    def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        self._path.unlink(missing_ok=True)

    def __enter__(self) -> BoundedUnixServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class BoundedUnixClient:
    """Bounded one-connection client for request/response and one-way local IPC."""

    def __init__(
        self,
        path: Path,
        *,
        peer_expectation: UnixSocketPeerExpectation,
        max_frame_bytes: int = RATE_FRAME_MAX_BYTES,
        timeout_seconds: float = 1.0,
    ) -> None:
        if (
            not path.is_absolute()
            or max_frame_bytes < 256
            or max_frame_bytes > UDS_FRAME_HARD_MAX_BYTES
        ):
            raise UdsProtocolError("CLIENT_CONFIGURATION_INVALID")
        if timeout_seconds <= 0 or timeout_seconds > 5:
            raise UdsProtocolError("CLIENT_CONFIGURATION_INVALID")
        self._path = path
        self._peer_expectation = peer_expectation
        self._max_frame_bytes = max_frame_bytes
        self._timeout_seconds = timeout_seconds

    def request(self, document: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._connected() as peer:
            peer.sendall(encode_frame(document, max_bytes=self._max_frame_bytes))
            peer.shutdown(socket.SHUT_WR)
            return receive_frame(peer, max_bytes=self._max_frame_bytes)

    def notify(self, document: Mapping[str, Any]) -> None:
        with self._connected() as peer:
            peer.sendall(encode_frame(document, max_bytes=self._max_frame_bytes))
            peer.shutdown(socket.SHUT_WR)
            if peer.recv(1) != NOTIFY_COMMITTED_ACK:
                raise UdsProtocolError("NOTIFY_COMMIT_NOT_ACKNOWLEDGED")
            if peer.recv(1) != b"":
                raise UdsProtocolError("NOTIFY_TRAILING_RESPONSE_FORBIDDEN")

    def _connected(self) -> socket.socket:
        self._validate_socket_path()
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.settimeout(self._timeout_seconds)
        try:
            peer.connect(str(self._path))
            credentials = peer_credentials(peer)
            self._validate_socket_path()
            if (
                credentials.uid != self._peer_expectation.peer_uid
                or credentials.gid != self._peer_expectation.peer_gid
            ):
                raise UdsProtocolError("SERVER_PEER_CREDENTIAL_MISMATCH")
        except BaseException:
            peer.close()
            raise
        return peer

    def _validate_socket_path(self) -> None:
        try:
            metadata = self._path.lstat()
        except OSError as exc:
            raise UdsProtocolError("SERVER_SOCKET_IDENTITY_MISMATCH") from exc
        expected = self._peer_expectation
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_ino != expected.inode
            or metadata.st_uid != expected.owner_uid
            or metadata.st_gid != expected.owner_gid
            or stat.S_IMODE(metadata.st_mode) != expected.mode
        ):
            raise UdsProtocolError("SERVER_SOCKET_IDENTITY_MISMATCH")
