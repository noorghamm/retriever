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
    name, rest = parse_name(payload)
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
    #header first, then the body streams disk-to-socket, never through RAM
    H.write_frame_header(cli_sock, H.T_OK, size)
    with open(name, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            cli_sock.sendall(chunk)
    log.info("%s GET %s ok (%d bytes)", peer, name, size)


def handle_put(cli_sock, peer, payload):
    #step 1: name + size arrive framed; the server approves or rejects
    #before any body bytes travel
    name, rest = parse_name(payload)
    if len(rest) != 8:
        raise H.FrameError(H.E_MALFORMED, "PUT payload must end with a u64 file size")
    file_size = int.from_bytes(rest, "big")

    if not H.check_filename(name):
        send_error(cli_sock, H.E_BAD_NAME, "invalid filename")
        log.info("%s PUT %r rejected: bad name", peer, name)
        return
    if file_size > H.MAX_FILE_SIZE:
        send_error(cli_sock, H.E_TOO_LARGE,
                   f"file exceeds the {H.MAX_FILE_SIZE} byte limit")
        log.info("%s PUT %s rejected: %d bytes too large", peer, name, file_size)
        return
    try:
        out = open(name, "xb")   #atomic claim; also our license to delete on failure
    except FileExistsError:
        send_error(cli_sock, H.E_ALREADY_EXISTS, "file already exists on server")
        log.info("%s PUT %s rejected: already exists", peer, name)
        return

    H.write_frame(cli_sock, H.T_OK)   #permission to send

    #step 2: exactly file_size raw bytes (no frame: TCP is already a
    #byte stream and the size was stated in step 1)
    written = 0
    head = b""
    try:
        with out:
            while written < file_size:
                chunk = H.read_exact_bytes(cli_sock, min(65536, file_size - written))
                if written == 0:
                    head = chunk[:8]
                out.write(chunk)
                written += len(chunk)
    except Exception:
        #we created this file (open "xb" succeeded), so removing it is safe
        os.remove(name)
        raise

    if H.is_image(name) and not (H.is_jpeg_header(head) or H.is_png_header(head)):
        log.warning("%s PUT %s: image extension but content is not JPEG/PNG", peer, name)

    H.write_frame(cli_sock, H.T_OK)
    log.info("%s PUT %s ok (%d bytes)", peer, name, written)


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
