from __future__ import annotations

import os
import socket
import struct
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from ai_quant.rate_budget.authorization import PeerCredentials
from ai_quant.services.uds import (
    NOTIFY_COMMITTED_ACK,
    BoundedUnixClient,
    BoundedUnixServer,
    UdsProtocolError,
    UnixSocketPeerExpectation,
    UnixSocketServerExpectation,
    encode_frame,
    receive_frame,
)


def _server(
    directory: Path,
    handler: Callable[[Mapping[str, Any], PeerCredentials], Mapping[str, Any] | None],
) -> BoundedUnixServer:
    directory.chmod(0o2770)
    return BoundedUnixServer(
        directory / "rate.sock",
        handler,
        identity_expectation=UnixSocketServerExpectation(
            runtime_directory_gid=directory.stat().st_gid,
            socket_owner_uid=os.geteuid(),
            socket_owner_gid=directory.stat().st_gid,
        ),
    )


def test_frame_round_trip_over_unix_socket() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        left.sendall(encode_frame({"message_type": "ReserveRequest", "message_id": "msg-00000001"}))
        assert receive_frame(right) == {
            "message_type": "ReserveRequest",
            "message_id": "msg-00000001",
        }
    finally:
        left.close()
        right.close()


def test_oversized_frame_is_rejected_before_payload_read() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        left.sendall(struct.pack("!I", 4097))
        with pytest.raises(UdsProtocolError, match="FRAME_SIZE_INVALID"):
            receive_frame(right, max_bytes=4096)
    finally:
        left.close()
        right.close()


def test_duplicate_json_key_is_rejected() -> None:
    payload = b'{"message_id":"one","message_id":"two"}'
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        left.sendall(struct.pack("!I", len(payload)) + payload)
        with pytest.raises(UdsProtocolError, match="DUPLICATE_JSON_KEY"):
            receive_frame(right)
    finally:
        left.close()
        right.close()


def test_non_object_json_is_rejected() -> None:
    payload = b"[]"
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        left.sendall(struct.pack("!I", len(payload)) + payload)
        with pytest.raises(UdsProtocolError, match="FRAME_DOCUMENT_NOT_OBJECT"):
            receive_frame(right)
    finally:
        left.close()
        right.close()


def test_server_refuses_world_accessible_runtime_directory(tmp_path: Path) -> None:
    tmp_path.chmod(0o777)
    server = BoundedUnixServer(
        tmp_path / "rate.sock",
        lambda request, peer: request,
        identity_expectation=UnixSocketServerExpectation(
            runtime_directory_gid=tmp_path.stat().st_gid,
            socket_owner_uid=os.geteuid(),
            socket_owner_gid=tmp_path.stat().st_gid,
        ),
    )
    with pytest.raises(UdsProtocolError, match="RUNTIME_DIRECTORY_UNSAFE"):
        server.start()


def test_server_socket_mode_and_cleanup(tmp_path: Path) -> None:
    socket_path = tmp_path / "rate.sock"
    server = _server(tmp_path, lambda request, peer: request)
    server.start()
    try:
        assert socket_path.stat().st_mode & 0o777 == 0o660
        assert server._listener is not None
        assert server._listener.family == socket.AF_UNIX
    finally:
        server.close()
    assert not socket_path.exists()


def test_server_acknowledges_one_way_message_only_after_handler_returns(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "rate.sock"
    server = _server(tmp_path, lambda request, peer: None)
    server.start()
    worker = threading.Thread(target=server.serve_one)
    worker.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(1)
        client.connect(str(socket_path))
        client.sendall(encode_frame({"message_type": "HeaderObservation"}))
        client.shutdown(socket.SHUT_WR)
        assert client.recv(1) == NOTIFY_COMMITTED_ACK
        assert client.recv(1) == b""
    finally:
        client.close()
        worker.join(timeout=1)
        server.close()
    assert not worker.is_alive()


def test_notify_rejects_eof_from_a_failed_handler(tmp_path: Path) -> None:
    socket_path = tmp_path / "rate.sock"

    def failed_handler(
        request: Mapping[str, Any],
        peer: PeerCredentials,
    ) -> None:
        raise ValueError("durable write failed")

    server = _server(tmp_path, failed_handler)
    server.start()
    metadata = socket_path.stat()
    client = BoundedUnixClient(
        socket_path,
        peer_expectation=UnixSocketPeerExpectation(
            inode=metadata.st_ino,
            owner_uid=metadata.st_uid,
            owner_gid=metadata.st_gid,
            peer_uid=os.geteuid(),
            peer_gid=os.getegid(),
        ),
    )
    errors: list[Exception] = []

    def serve_failed_request() -> None:
        try:
            server.serve_one()
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=serve_failed_request)
    worker.start()
    try:
        with pytest.raises(UdsProtocolError, match="NOTIFY_COMMIT_NOT_ACKNOWLEDGED"):
            client.notify({"message_type": "SendOutcome"})
        worker.join(timeout=1)
    finally:
        server.close()
    assert not worker.is_alive()
    assert len(errors) == 1


def test_bounded_client_supports_request_and_one_way_message(tmp_path: Path) -> None:
    socket_path = tmp_path / "rate.sock"
    responses = iter(({"status": "ok"}, None))
    server = _server(tmp_path, lambda request, peer: next(responses))
    server.start()
    metadata = socket_path.stat()
    client = BoundedUnixClient(
        socket_path,
        peer_expectation=UnixSocketPeerExpectation(
            inode=metadata.st_ino,
            owner_uid=metadata.st_uid,
            owner_gid=metadata.st_gid,
            peer_uid=os.geteuid(),
            peer_gid=os.getegid(),
        ),
    )
    try:
        worker = threading.Thread(target=server.serve_one)
        worker.start()
        assert client.request({"message_type": "ReserveRequest"}) == {"status": "ok"}
        worker.join(timeout=1)
        worker = threading.Thread(target=server.serve_one)
        worker.start()
        client.notify({"message_type": "SendOutcome"})
        worker.join(timeout=1)
    finally:
        server.close()
    assert not worker.is_alive()


def test_bounded_client_rejects_a_replaced_server_socket(tmp_path: Path) -> None:
    socket_path = tmp_path / "rate.sock"
    original = _server(tmp_path, lambda request, peer: request)
    original.start()
    metadata = socket_path.stat()
    client = BoundedUnixClient(
        socket_path,
        peer_expectation=UnixSocketPeerExpectation(
            inode=metadata.st_ino,
            owner_uid=metadata.st_uid,
            owner_gid=metadata.st_gid,
            peer_uid=os.geteuid(),
            peer_gid=os.getegid(),
        ),
    )
    original.close()
    # Keep the released inode occupied so the replacement cannot inherit the identity.
    (tmp_path / "released-inode-occupier").touch()
    replacement = _server(tmp_path, lambda request, peer: request)
    replacement.start()
    assert socket_path.stat().st_ino != metadata.st_ino
    try:
        with pytest.raises(UdsProtocolError, match="SERVER_SOCKET_IDENTITY_MISMATCH"):
            client.request({"message_type": "ReserveRequest"})
    finally:
        replacement.close()


def test_bounded_client_rejects_unexpected_server_credentials(tmp_path: Path) -> None:
    socket_path = tmp_path / "rate.sock"
    server = _server(tmp_path, lambda request, peer: request)
    server.start()
    metadata = socket_path.stat()
    client = BoundedUnixClient(
        socket_path,
        peer_expectation=UnixSocketPeerExpectation(
            inode=metadata.st_ino,
            owner_uid=metadata.st_uid,
            owner_gid=metadata.st_gid,
            peer_uid=os.geteuid() + 1,
            peer_gid=os.getegid(),
        ),
    )
    try:
        with pytest.raises(UdsProtocolError, match="SERVER_PEER_CREDENTIAL_MISMATCH"):
            client.request({"message_type": "ReserveRequest"})
    finally:
        server.close()
