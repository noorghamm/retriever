"""Session behavior: one connection, many commands, QUIT to finish."""
import socket
import threading
import time

import pytest

from conftest import connect_v2, free_port, name_payload
from retriever import protocol as H
from retriever import server as server_mod
from retriever.server import start_server


def test_many_commands_down_one_connection(server_port, tmp_path):
    (tmp_path / "a.txt").write_bytes(b"alpha")

    with connect_v2(server_port) as sock:
        #command 1: LIST
        H.write_frame(sock, H.T_LIST)
        frame_type, payload = H.read_frame(sock)
        assert frame_type == H.T_OK
        assert b"a.txt" in payload

        #command 2: GET on the same connection
        H.write_frame(sock, H.T_GET, name_payload(b"a.txt"))
        frame_type, size = H.read_frame_header(sock)
        assert frame_type == H.T_OK
        assert H.read_exact_bytes(sock, size) == b"alpha"

        #command 3: PUT, still the same connection
        H.write_frame(sock, H.T_PUT, name_payload(b"b.txt", 4))
        assert H.read_frame(sock)[0] == H.T_OK
        sock.sendall(b"beta")
        assert H.read_frame(sock)[0] == H.T_OK

        #and out politely
        H.write_frame(sock, H.T_QUIT)
        assert H.read_frame(sock)[0] == H.T_OK

    assert (tmp_path / "b.txt").read_bytes() == b"beta"


def test_quit_closes_the_connection(server_port):
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_QUIT)
        assert H.read_frame(sock)[0] == H.T_OK
        sock.settimeout(3)
        assert sock.recv(1) == b""   #server hung up after its OK


def test_application_error_does_not_end_the_session(server_port):
    """NOT_FOUND is the file's problem, not the connection's."""
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_GET, name_payload(b"ghost.txt"))
        frame_type, payload = H.read_frame(sock)
        assert frame_type == H.T_ERROR
        assert H.unpack_error(payload)[0] == H.E_NOT_FOUND

        #the session must still be alive and serving
        H.write_frame(sock, H.T_LIST)
        assert H.read_frame(sock)[0] == H.T_OK

        H.write_frame(sock, H.T_QUIT)
        assert H.read_frame(sock)[0] == H.T_OK


def test_framing_violation_ends_the_session(server_port):
    """After a FrameError the byte stream is unknowable; server must close."""
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_OK)   #a reply type from a client is nonsense
        frame_type, payload = H.read_frame(sock)
        assert frame_type == H.T_ERROR
        assert H.unpack_error(payload)[0] == H.E_MALFORMED
        sock.settimeout(3)
        assert sock.recv(1) == b""   #connection is gone, not just scolded


def test_two_sessions_alive_at_once(server_port):
    """The proof of concurrency: yesterday's sequential server could
    never answer session B while session A was still open."""
    a = connect_v2(server_port)
    try:
        with connect_v2(server_port) as b:     #hangs forever if sequential
            H.write_frame(b, H.T_LIST)
            assert H.read_frame(b)[0] == H.T_OK
            H.write_frame(b, H.T_QUIT)
            assert H.read_frame(b)[0] == H.T_OK

        #A was untouched throughout and still works
        H.write_frame(a, H.T_LIST)
        assert H.read_frame(a)[0] == H.T_OK
        H.write_frame(a, H.T_QUIT)
        assert H.read_frame(a)[0] == H.T_OK
    finally:
        a.close()


def test_session_cap_queues_excess_clients(tmp_path, monkeypatch):
    """Over MAX_CLIENTS, new connections wait their turn, never error."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server_mod, "MAX_CLIENTS", 1)
    port = free_port()
    threading.Thread(target=start_server, args=(port,), daemon=True).start()
    deadline = time.time() + 5
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if time.time() > deadline:
                pytest.fail("server did not start listening within 5s")
            time.sleep(0.05)

    a = connect_v2(port)                        #occupies the only slot

    #B connects (TCP accepts into the backlog) but gets no HELLO reply
    #while A holds the slot
    b = socket.create_connection(("127.0.0.1", port), timeout=5)
    H.write_frame(b, H.T_HELLO)
    b.settimeout(0.5)
    with pytest.raises(socket.timeout):
        H.read_frame(b)

    #A leaves; B's session must now begin
    H.write_frame(a, H.T_QUIT)
    assert H.read_frame(a)[0] == H.T_OK
    a.close()

    b.settimeout(5)
    assert H.read_frame(b)[0] == H.T_OK         #the delayed HELLO reply
    H.write_frame(b, H.T_QUIT)
    assert H.read_frame(b)[0] == H.T_OK
    b.close()


def test_closing_without_quit_is_tolerated(server_port):
    """An impolite client must not wedge the (still sequential) server."""
    sock = connect_v2(server_port)
    sock.close()   #no QUIT, just gone

    #server should move straight on to the next client
    with connect_v2(server_port) as sock2:
        H.write_frame(sock2, H.T_LIST)
        assert H.read_frame(sock2)[0] == H.T_OK
        H.write_frame(sock2, H.T_QUIT)
        assert H.read_frame(sock2)[0] == H.T_OK
