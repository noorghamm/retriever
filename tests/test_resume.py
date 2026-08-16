"""Resume and integrity: transfers that die on purpose and come back.

The Proxy below sits between client and server so a test can cut a
connection at an exact byte, or flip a bit in flight, which is the only
honest way to prove resume and hash verification actually work.
"""
import os
import socket
import threading
import time

import pytest

from conftest import connect_v2, free_port, get_payload, put_payload, read_get, sha
from retriever import client
from retriever import protocol as H


class Proxy:
    """A TCP proxy that can sabotage the traffic passing through it.

    cut_after: close the connection once this many bytes have flowed
               from the server to the client.
    corrupt_at: flip one bit at this byte offset of the server-to-client
                stream (or client-to-server when direction is "up").
    """

    def __init__(self, target_port, cut_after=None, corrupt_at=None, direction="down"):
        self.target_port = target_port
        self.cut_after = cut_after
        self.corrupt_at = corrupt_at
        self.direction = direction
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _pump(self, src, dst, sabotage):
        sent = 0
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                if sabotage and self.corrupt_at is not None:
                    lo, hi = sent, sent + len(chunk)
                    if lo <= self.corrupt_at < hi:
                        i = self.corrupt_at - lo
                        chunk = chunk[:i] + bytes([chunk[i] ^ 0x01]) + chunk[i + 1:]
                if sabotage and self.cut_after is not None \
                        and sent + len(chunk) >= self.cut_after:
                    dst.sendall(chunk[:self.cut_after - sent])
                    break
                dst.sendall(chunk)
                sent += len(chunk)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _run(self):
        try:
            client_side, _ = self.listener.accept()
        except OSError:
            return
        server_side = socket.create_connection(("127.0.0.1", self.target_port), timeout=10)
        down = threading.Thread(
            target=self._pump,
            args=(server_side, client_side, self.direction == "down"),
            daemon=True)
        up = threading.Thread(
            target=self._pump,
            args=(client_side, server_side, self.direction == "up"),
            daemon=True)
        down.start()
        up.start()
        down.join()
        up.join()

    def close(self):
        try:
            self.listener.close()
        except OSError:
            pass


def _wait_for_partial(directory, expected_size, timeout=5):
    """Wait until the server has flushed and released a partial of this size."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for p in directory.iterdir():
            if p.name.endswith(".part") and p.stat().st_size == expected_size:
                time.sleep(0.05)   # let the handler thread release its slot
                return p
        time.sleep(0.02)
    raise AssertionError(f"no partial of {expected_size} bytes appeared")


#--- protocol-level resume --------------------------------------------------

def test_get_resumes_from_offset(server_port, tmp_path):
    body = bytes(range(256)) * 40      # 10,240 bytes
    (tmp_path / "big.bin").write_bytes(body)
    digest = sha(body)

    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_GET,
                      get_payload(b"big.bin", offset=4000, token=digest[:H.TOKEN_SIZE]))
        frame_type, (total, start, got_digest), tail = read_get(sock)

    assert frame_type == H.T_OK
    assert (total, start) == (len(body), 4000)
    assert got_digest == digest
    assert tail == body[4000:]         # exactly the missing remainder


def test_get_with_stale_token_restarts_from_zero(server_port, tmp_path):
    """The client's partial belongs to a file the server no longer has,
    so the server must ignore the offset rather than splice the join."""
    body = b"the current contents"
    (tmp_path / "changed.bin").write_bytes(body)

    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_GET,
                      get_payload(b"changed.bin", offset=5, token=b"\xde\xad\xbe\xef" * 2))
        frame_type, (total, start, digest), got = read_get(sock)

    assert frame_type == H.T_OK
    assert start == 0, "server honored an offset for the wrong file"
    assert got == body
    assert digest == sha(body)


def test_put_resumes_where_it_died(server_port, tmp_path):
    body = b"".join(bytes([i % 256]) for i in range(9000))
    digest = sha(body)

    #attempt 1: die after 3,000 bytes
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_PUT, put_payload(b"resume.bin", len(body), digest))
        frame_type, payload = H.read_frame(sock)
        assert frame_type == H.T_OK
        assert int.from_bytes(payload[:8], "big") == 0    # nothing held yet
        sock.sendall(body[:3000])
    #connection dropped; wait for the server to finish releasing the partial
    _wait_for_partial(tmp_path, 3000)

    #attempt 2: the server should ask only for what it is missing
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_PUT, put_payload(b"resume.bin", len(body), digest))
        frame_type, payload = H.read_frame(sock)
        assert frame_type == H.T_OK
        resume_offset = int.from_bytes(payload[:8], "big")
        assert resume_offset == 3000, "server forgot the bytes it already had"
        sock.sendall(body[resume_offset:])
        assert H.read_frame(sock)[0] == H.T_OK

    assert (tmp_path / "resume.bin").read_bytes() == body
    assert [p.name for p in tmp_path.iterdir()] == ["resume.bin"]   # partial cleaned up


def test_resuming_a_different_file_does_not_splice(server_port, tmp_path):
    """The corruption trap: same name, different content. The second
    upload must NOT append onto the first one's bytes."""
    first = b"A" * 5000
    second = b"B" * 5000

    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_PUT, put_payload(b"swap.bin", len(first), sha(first)))
        assert H.read_frame(sock)[0] == H.T_OK
        sock.sendall(first[:2000])      # die partway
    _wait_for_partial(tmp_path, 2000)
    #now upload DIFFERENT content under the same name
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_PUT, put_payload(b"swap.bin", len(second), sha(second)))
        frame_type, payload = H.read_frame(sock)
        assert frame_type == H.T_OK
        assert int.from_bytes(payload[:8], "big") == 0, \
            "server offered to resume a different file's bytes"
        sock.sendall(second)
        assert H.read_frame(sock)[0] == H.T_OK

    assert (tmp_path / "swap.bin").read_bytes() == second


def test_stale_partials_are_swept(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fresh = tmp_path / ".fresh.aabbccddeeff0011.part"
    stale = tmp_path / ".stale.aabbccddeeff0011.part"
    fresh.write_bytes(b"new")
    stale.write_bytes(b"old")
    os.utime(stale, (0, 0))    # 1970: definitively abandoned

    removed = H.sweep_partials()

    assert removed == 1
    assert fresh.exists() and not stale.exists()


#--- integrity under sabotage ------------------------------------------------

def test_corrupted_download_is_caught_by_the_hash(server_port, tmp_path, monkeypatch):
    body = b"important data " * 500
    (tmp_path / "src.bin").write_bytes(body)

    work = tmp_path / "clientside"
    work.mkdir()
    dest = str(work / "copy.bin")

    #flip a bit deep inside the body as it flows to the client
    proxy = Proxy(server_port, corrupt_at=H.HEADER_SIZE + H.GET_META + 2000)
    sock = client.connect("127.0.0.1", proxy.port)
    with pytest.raises(SystemExit) as exc:
        client.do_get(sock, "src.bin", output=dest)
    sock.close()
    proxy.close()

    assert exc.value.code == client.EXIT_CONN
    assert not (work / "copy.bin").exists(), "corrupt download was presented as valid"
    assert list(work.iterdir()) == [], "corrupt partial should be discarded, not resumed"


def test_corrupted_upload_is_rejected_by_the_server(server_port, tmp_path, monkeypatch):
    body = b"upload payload " * 400
    work = tmp_path / "clientside"
    work.mkdir()
    (work / "src.bin").write_bytes(body)

    #flip a bit inside the body on its way up
    proxy = Proxy(server_port, corrupt_at=H.HEADER_SIZE + H.PUT_PREFIX + 9 + 1000,
                  direction="up")
    sock = client.connect("127.0.0.1", proxy.port)
    with pytest.raises(SystemExit) as exc:
        client.do_put(sock, str(work / "src.bin"))
    sock.close()
    proxy.close()

    assert exc.value.code == client.EXIT_SERVER
    assert not (tmp_path / "src.bin").exists(), "corrupt upload was published"


def test_murdered_download_resumes_and_verifies(server_port, tmp_path, monkeypatch):
    """End to end through the real client: kill a download partway, run
    it again, and require a byte-perfect, hash-verified result."""
    body = os.urandom(200_000)
    (tmp_path / "movie.bin").write_bytes(body)

    work = tmp_path / "clientside"
    work.mkdir()
    dest = str(work / "movie.bin")

    #attempt 1: cut the connection after ~60,000 bytes reach the client
    proxy = Proxy(server_port, cut_after=60_000)
    sock = client.connect("127.0.0.1", proxy.port)
    with pytest.raises(SystemExit) as exc:
        client.do_get(sock, "movie.bin", output=dest)
    sock.close()
    proxy.close()
    assert exc.value.code == client.EXIT_CONN

    partial = H.find_partial(dest)
    assert partial, "no partial left to resume from"
    salvaged = os.path.getsize(partial)
    assert 0 < salvaged < len(body)

    #attempt 2: straight to the server, which should send only the rest
    sock = client.connect("127.0.0.1", server_port)
    client.do_get(sock, "movie.bin", output=dest)
    sock.close()

    assert (work / "movie.bin").read_bytes() == body
    assert H.find_partial(dest) is None, "partial not cleaned up after success"


@pytest.mark.slow
def test_murdered_100mb_transfer_resumes_and_verifies(server_port, tmp_path, monkeypatch):
    """Phase 3's definition of done, at the size it was written for.

    Marked slow: the logic is identical at 200 KB (see above), so the
    everyday suite runs the fast version. Run this one with -m slow.
    """
    size = 100 * 1024 * 1024
    chunk = os.urandom(1024 * 1024)
    src = tmp_path / "huge.bin"
    with open(src, "wb") as f:
        for _ in range(size // len(chunk)):
            f.write(chunk)
    digest = H.sha256_file(src)

    work = tmp_path / "clientside"
    work.mkdir()
    dest = str(work / "huge.bin")

    proxy = Proxy(server_port, cut_after=40 * 1024 * 1024)
    sock = client.connect("127.0.0.1", proxy.port)
    with pytest.raises(SystemExit):
        client.do_get(sock, "huge.bin", output=dest)
    sock.close()
    proxy.close()

    partial = H.find_partial(dest)
    assert partial and 0 < os.path.getsize(partial) < size

    sock = client.connect("127.0.0.1", server_port)
    client.do_get(sock, "huge.bin", output=dest)
    sock.close()

    assert os.path.getsize(work / "huge.bin") == size
    assert H.sha256_file(work / "huge.bin") == digest
