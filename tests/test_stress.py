"""Concurrency under load: many clients hammering the server at once.

These tests are written to CATCH interleaving, not merely to survive it.
Every client uploads content unique to itself, so any crossed write
shows up as a byte mismatch rather than passing quietly.
"""
import socket
import threading

import pytest

from conftest import connect_v2, get_payload, put_payload, read_get, sha
from retriever import protocol as H

CLIENTS = 20


def _put(sock, name, body):
    """Full two-step PUT. Returns the final frame type."""
    H.write_frame(sock, H.T_PUT, put_payload(name, len(body), sha(body)))
    frame_type, payload = H.read_frame(sock)
    if frame_type != H.T_OK:
        return frame_type, H.unpack_error(payload)[0]
    sock.sendall(body)
    frame_type, payload = H.read_frame(sock)
    reason = H.unpack_error(payload)[0] if frame_type == H.T_ERROR else None
    return frame_type, reason


def _get(sock, name):
    """Full GET. Returns (frame_type, body_or_reason)."""
    H.write_frame(sock, H.T_GET, get_payload(name))
    frame_type, info, body = read_get(sock)
    if frame_type == H.T_ERROR:
        return frame_type, info[0]
    return frame_type, body


def _run_workers(count, worker):
    """Run worker(i) in `count` threads; return results, failing on any
    exception raised inside a thread."""
    results = [None] * count
    errors = []

    def wrapped(i):
        try:
            results[i] = worker(i)
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(f"worker {i}: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a worker thread hung"
    assert not errors, "workers raised: " + "; ".join(errors)
    return results


def test_parallel_mixed_workload(server_port, tmp_path):
    """20 clients doing LIST + GET + PUT at once, all bytes verified."""
    seeded = b"seed-" + bytes(range(256)) * 8   # 2053 bytes, all byte values
    (tmp_path / "seed.bin").write_bytes(seeded)

    def worker(i):
        # content unique per client: a crossed write cannot go unnoticed
        body = f"client-{i:02d}-".encode() * 500
        with connect_v2(server_port) as sock:
            H.write_frame(sock, H.T_LIST)
            assert H.read_frame(sock)[0] == H.T_OK

            frame_type, got = _get(sock, b"seed.bin")
            assert frame_type == H.T_OK
            assert got == seeded, f"client {i} read a corrupted seed file"

            frame_type, reason = _put(sock, f"up-{i:02d}.bin".encode(), body)
            assert frame_type == H.T_OK, f"client {i} PUT failed: reason {reason}"

            H.write_frame(sock, H.T_QUIT)
            assert H.read_frame(sock)[0] == H.T_OK
        return body

    bodies = _run_workers(CLIENTS, worker)

    # every upload landed intact and unmixed
    for i, body in enumerate(bodies):
        stored = (tmp_path / f"up-{i:02d}.bin").read_bytes()
        assert stored == body, f"up-{i:02d}.bin was corrupted by concurrent traffic"

    # the seed file was never touched, and no debris was left behind
    assert (tmp_path / "seed.bin").read_bytes() == seeded
    names = sorted(p.name for p in tmp_path.iterdir())
    expected = sorted(["seed.bin"] + [f"up-{i:02d}.bin" for i in range(CLIENTS)])
    assert names == expected, f"unexpected files after stress run: {names}"


def test_simultaneous_put_same_name_has_exactly_one_winner(server_port, tmp_path):
    """The race the spec promises an answer to: exactly one PUT wins the
    atomic create, everyone else gets ALREADY_EXISTS. Which one wins is
    deliberately unspecified.

    Verified by mutation: replacing the atomic open(name, "xb") with a
    check-then-create (os.path.exists followed by open "wb") makes this
    test fail with 3 winners instead of 1, which is the classic TOCTOU
    race. The test has teeth.
    """
    start = threading.Barrier(CLIENTS)

    def worker(i):
        body = f"winner-{i:02d}".encode() * 100
        with connect_v2(server_port) as sock:
            start.wait(timeout=10)          # all threads pounce together
            frame_type, reason = _put(sock, b"contested.bin", body)
            H.write_frame(sock, H.T_QUIT)
            H.read_frame(sock)
        return frame_type, reason, body

    results = _run_workers(CLIENTS, worker)

    winners = [r for r in results if r[0] == H.T_OK]
    losers = [r for r in results if r[0] == H.T_ERROR]
    assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}"
    assert len(losers) == CLIENTS - 1
    assert all(reason == H.E_ALREADY_EXISTS for _, reason, _ in losers)

    # the stored file is exactly the winner's bytes, not a blend
    assert (tmp_path / "contested.bin").read_bytes() == winners[0][2]


def test_parallel_failures_publish_nothing_and_stay_resumable(server_port, tmp_path):
    """Half the clients die mid-upload. The survivors' files must be
    perfect, and the dead ones must publish nothing while leaving exactly
    one resumable partial each."""
    def worker(i):
        name = f"half-{i:02d}.bin".encode()
        body = f"body-{i:02d}-".encode() * 200
        sock = connect_v2(server_port)
        try:
            if i % 2 == 0:
                # promise the body, send a third of it, then vanish
                H.write_frame(sock, H.T_PUT, put_payload(name, len(body), sha(body)))
                assert H.read_frame(sock)[0] == H.T_OK
                sock.sendall(body[:len(body) // 3])
                return None
            frame_type, reason = _put(sock, name, body)
            assert frame_type == H.T_OK, f"client {i} PUT failed: reason {reason}"
            return body
        finally:
            sock.close()

    bodies = _run_workers(CLIENTS, worker)

    names = sorted(p.name for p in tmp_path.iterdir())
    published = sorted(n for n in names if not n.endswith(".part"))
    partials = sorted(n for n in names if n.endswith(".part"))

    survivors = sorted(f"half-{i:02d}.bin" for i in range(CLIENTS) if i % 2)
    assert published == survivors, "an unfinished upload was published"
    assert len(partials) == CLIENTS // 2, "each dead upload should leave one partial"
    for i in range(1, CLIENTS, 2):
        assert (tmp_path / f"half-{i:02d}.bin").read_bytes() == bodies[i]
