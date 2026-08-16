import sys
import os
import socket
import logging
import threading

from retriever import protocol as H

log = logging.getLogger("retriever.server")

#how many client sessions may run at once; further connections queue
#in the TCP accept backlog until a slot frees
MAX_CLIENTS = 32

#partial files currently being written, guarded by _in_flight_lock, so
#two clients cannot interleave appends into the same partial
_in_flight = set()
_in_flight_lock = threading.Lock()


def create_server_socket(port):
    """Create a TCP socket bound to every interface on the given port."""
    srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        #prevents "Address already in use" when restarting the server
        srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv_sock.bind(("0.0.0.0", port))
        srv_sock.listen(64)   #room for connections waiting on a session slot
        log.info("listening on 0.0.0.0:%d", port)
    except OSError as e:
        log.error("cannot bind port %d: %s", port, e)
        sys.exit(1)
    return srv_sock


def send_error(sock, reason, message):
    H.write_frame(sock, H.T_ERROR, H.pack_error(reason, message))


def handle_client(cli_sock, cli_addr):
    """One v2 session: HELLO, any number of commands, QUIT (or close)."""
    peer = "{}:{}".format(*cli_addr)
    try:
        frame_type, _ = H.read_frame(cli_sock)
        if frame_type != H.T_HELLO:
            raise H.FrameError(H.E_MALFORMED, "expected HELLO before any command")
        H.write_frame(cli_sock, H.T_OK)

        while True:
            try:
                frame_type, payload = H.read_frame(cli_sock)
            except ConnectionError:
                #closing instead of QUIT is legal, just less polite
                log.info("%s session ended without QUIT", peer)
                break
            if frame_type == H.T_QUIT:
                H.write_frame(cli_sock, H.T_OK)
                log.info("%s QUIT", peer)
                break
            elif frame_type == H.T_LIST:
                handle_list(cli_sock, peer)
            elif frame_type == H.T_GET:
                handle_get(cli_sock, peer, payload)
            elif frame_type == H.T_PUT:
                handle_put(cli_sock, peer, payload)
            else:
                raise H.FrameError(H.E_MALFORMED, "expected a command frame")

    except H.FrameError as e:
        log.warning("%s protocol violation: %s", peer, e)
        try:
            send_error(cli_sock, e.reason, str(e))
        except OSError:
            pass
    except socket.timeout:
        log.warning("%s timed out: no data for %ss", peer, H.SOCKET_TIMEOUT)
    except (ConnectionError, ConnectionResetError) as e:
        log.info("%s disconnected: %s", peer, e)
    except Exception:
        log.exception("%s internal error", peer)
        try:
            send_error(cli_sock, H.E_INTERNAL, "internal server error")
        except OSError:
            pass
    finally:
        cli_sock.close()
        log.debug("%s connection closed", peer)


def parse_name(payload):
    """Split a payload starting with u16 name_len + name bytes.
    Returns (name, remaining bytes)."""
    if len(payload) < 2:
        raise H.FrameError(H.E_MALFORMED, "payload too short for a name length")
    name_len = int.from_bytes(payload[:2], "big")
    if len(payload) < 2 + name_len:
        raise H.FrameError(H.E_MALFORMED, "payload shorter than its name_len claims")
    name = payload[2:2 + name_len].decode("utf-8", errors="replace")
    return name, payload[2 + name_len:]


def handle_list(cli_sock, peer):
    names = os.listdir(".")
    body = b"\0".join(n.encode("utf-8") for n in names)
    H.write_frame(cli_sock, H.T_OK, body)
    log.info("%s LIST ok (%d entries)", peer, len(names))


def handle_get(cli_sock, peer, payload):
    #v3 layout: u64 offset, 8-byte resume token, u16 name_len, name
    if len(payload) < H.GET_PREFIX:
        raise H.FrameError(H.E_MALFORMED, "GET payload shorter than its fixed fields")
    offset = int.from_bytes(payload[:8], "big")
    token = payload[8:16]
    name, rest = parse_name(payload[16:])
    if rest:
        raise H.FrameError(H.E_MALFORMED, "trailing bytes after GET name")

    if not H.check_filename(name):
        send_error(cli_sock, H.E_BAD_NAME, "invalid filename")
        log.info("%s GET %r rejected: bad name", peer, name)
        return
    if not os.path.isfile(name):
        send_error(cli_sock, H.E_NOT_FOUND, "not found on server")
        log.info("%s GET %s rejected: not found", peer, name)
        return

    size = os.path.getsize(name)
    if offset > size:
        raise H.FrameError(H.E_MALFORMED, "GET offset is past the end of the file")
    digest = H.sha256_file(name)

    #honor the offset only if the client is resuming THIS file; otherwise
    #its partial data belongs to a file we no longer have, so start over
    start = offset if offset and token == digest[:H.TOKEN_SIZE] else 0
    if offset and start == 0:
        log.info("%s GET %s resume token stale, restarting from 0", peer, name)

    body_len = size - start
    H.write_frame_header(cli_sock, H.T_OK, H.GET_META + body_len)
    cli_sock.sendall(size.to_bytes(8, "big") + start.to_bytes(8, "big") + digest)
    with open(name, "rb") as f:
        f.seek(start)
        remaining = body_len
        while remaining > 0:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                break
            cli_sock.sendall(chunk)
            remaining -= len(chunk)
    log.info("%s GET %s ok (%d bytes from offset %d)", peer, name, body_len, start)


def handle_put(cli_sock, peer, payload):
    #v3 step 1: u64 file_size, 32-byte hash, u16 name_len, name. The
    #server approves or rejects before any body byte travels.
    if len(payload) < H.PUT_PREFIX:
        raise H.FrameError(H.E_MALFORMED, "PUT payload shorter than its fixed fields")
    file_size = int.from_bytes(payload[:8], "big")
    digest = payload[8:8 + H.HASH_SIZE]
    name, rest = parse_name(payload[8 + H.HASH_SIZE:])
    if rest:
        raise H.FrameError(H.E_MALFORMED, "trailing bytes after PUT name")

    if not H.check_filename(name):
        send_error(cli_sock, H.E_BAD_NAME, "invalid filename")
        log.info("%s PUT %r rejected: bad name", peer, name)
        return
    if file_size > H.MAX_FILE_SIZE:
        send_error(cli_sock, H.E_TOO_LARGE,
                   f"file exceeds the {H.MAX_FILE_SIZE} byte limit")
        log.info("%s PUT %s rejected: %d bytes too large", peer, name, file_size)
        return
    #advisory: the authoritative check is the link at publication time
    if os.path.exists(name):
        send_error(cli_sock, H.E_ALREADY_EXISTS, "file already exists on server")
        log.info("%s PUT %s rejected: already exists", peer, name)
        return

    partial = H.partial_name(name, digest)
    with _in_flight_lock:
        if partial in _in_flight:
            send_error(cli_sock, H.E_ALREADY_EXISTS, "an upload of this file is in progress")
            log.info("%s PUT %s rejected: already in flight", peer, name)
            return
        _in_flight.add(partial)
    try:
        _receive_upload(cli_sock, peer, name, partial, file_size, digest)
    finally:
        with _in_flight_lock:
            _in_flight.discard(partial)


def _receive_upload(cli_sock, peer, name, partial, file_size, digest):
    resume_offset = os.path.getsize(partial) if os.path.exists(partial) else 0
    if resume_offset > file_size:
        #a partial larger than the file it claims to be cannot be ours
        os.remove(partial)
        resume_offset = 0

    H.write_frame(cli_sock, H.T_OK, resume_offset.to_bytes(8, "big"))
    if resume_offset:
        log.info("%s PUT %s resuming at byte %d", peer, name, resume_offset)

    #step 2: exactly file_size - resume_offset raw bytes. A failure here
    #KEEPS the partial: that is what makes the next attempt resumable.
    #Note we recv directly rather than using read_exact_bytes: that helper
    #is all-or-nothing, which is right for frame headers and wrong here,
    #because every byte that arrives must survive to be resumed from.
    written = resume_offset
    with open(partial, "ab") as out:
        while written < file_size:
            chunk = cli_sock.recv(min(65536, file_size - written))
            if not chunk:
                raise ConnectionError("connection closed early during upload")
            out.write(chunk)
            written += len(chunk)

    if H.sha256_file(partial) != digest:
        os.remove(partial)
        send_error(cli_sock, H.E_CORRUPT, "content did not match the stated hash")
        log.warning("%s PUT %s rejected: hash mismatch", peer, name)
        return

    with open(partial, "rb") as f:
        head = f.read(8)
    if H.is_image(name) and not (H.is_jpeg_header(head) or H.is_png_header(head)):
        log.warning("%s PUT %s: image extension but content is not JPEG/PNG", peer, name)

    #publication is atomic and never overwrites: link fails if the name
    #was taken while this upload was in flight
    try:
        os.link(partial, name)
    except FileExistsError:
        os.remove(partial)
        send_error(cli_sock, H.E_ALREADY_EXISTS, "file already exists on server")
        log.info("%s PUT %s lost the publication race", peer, name)
        return
    os.remove(partial)

    H.write_frame(cli_sock, H.T_OK)
    log.info("%s PUT %s ok (%d bytes, verified)", peer, name, written)


def _serve(cli_sock, cli_addr, gate):
    try:
        handle_client(cli_sock, cli_addr)
    finally:
        gate.release()


def start_server(port):
    """The main server loop: accept, then hand each client its own thread.

    A semaphore caps concurrent sessions at MAX_CLIENTS; when full, the
    accept loop simply waits, so excess connections queue in the listen
    backlog instead of exhausting server resources.
    """
    srv_sock = create_server_socket(port)
    swept = H.sweep_partials()
    if swept:
        log.info("removed %d abandoned partial upload(s)", swept)
    gate = threading.Semaphore(MAX_CLIENTS)
    try:
        while True:
            try:
                cli_sock, cli_addr = srv_sock.accept()

                #a silent peer must not hold a session slot forever
                cli_sock.settimeout(H.SOCKET_TIMEOUT)
                gate.acquire()
                threading.Thread(
                    target=_serve,
                    args=(cli_sock, cli_addr, gate),
                    name="client-{}:{}".format(*cli_addr),
                    daemon=True,
                ).start()
            except Exception as e:
                log.error("server error: %s", e)
    except KeyboardInterrupt:
        log.info("server stopped by keyboard interrupt")
    finally:
        srv_sock.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 -m retriever.server <port>")
        sys.exit(1)
    try:
        port = int(sys.argv[1])
        if not (1024 <= port <= 65535):
            print("port must be between 1024 and 65535")
            sys.exit(1)
    except ValueError:
        print("Invalid port number. Please enter a valid integer for the port")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    )
    start_server(port)
