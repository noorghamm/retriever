import argparse
import os
import socket
import sys

from retriever import protocol as H

#exit codes, one per failure class, so scripts can react
EXIT_OK = 0
EXIT_SERVER = 1   #server refused the request (ERROR frame)
EXIT_LOCAL = 2    #problem on our side of the disk
EXIT_CONN = 3     #connection or protocol failure


def fail(code, message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def connect(host, port):
    """Open a TCP connection and complete the v2 HELLO handshake."""
    try:
        sock = socket.create_connection((host, port), timeout=H.SOCKET_TIMEOUT)
    except OSError as e:
        fail(EXIT_CONN, f"cannot connect to {host}:{port}: {e}")
    sock.settimeout(H.SOCKET_TIMEOUT)
    H.write_frame(sock, H.T_HELLO)
    frame_type, payload = H.read_frame(sock)
    if frame_type == H.T_ERROR:
        reason, message = H.unpack_error(payload)
        fail(EXIT_SERVER, f"server refused connection: {message}")
    return sock


def _read_error_payload(sock, payload_len):
    """After read_frame_header saw an ERROR frame, fetch and parse it."""
    if payload_len > H.PAYLOAD_CAPS[H.T_ERROR]:
        raise H.FrameError(H.E_MALFORMED, "oversized error frame from server")
    return H.unpack_error(H.read_exact_bytes(sock, payload_len))


def do_list(sock):
    H.write_frame(sock, H.T_LIST)
    frame_type, payload = H.read_frame(sock)
    if frame_type == H.T_ERROR:
        _, message = H.unpack_error(payload)
        fail(EXIT_SERVER, message)
    names = [n.decode("utf-8") for n in payload.split(b"\x00") if n]
    for name in sorted(names):
        print(name)
    if not names:
        print("(server directory is empty)", file=sys.stderr)


def do_get(sock, filename, output=None):
    dest = output or os.path.basename(filename)
    if os.path.exists(dest):
        fail(EXIT_LOCAL, f"'{dest}' already exists locally, use -o to pick another name")

    #a leftover partial from an interrupted attempt carries its own
    #resume token in its filename, which is what we offer the server
    existing = H.find_partial(dest)
    offset = os.path.getsize(existing) if existing else 0
    token = (H.partial_token(existing) or b"\0" * H.TOKEN_SIZE) if existing \
        else b"\0" * H.TOKEN_SIZE

    name_bytes = filename.encode("utf-8")
    H.write_frame(
        sock, H.T_GET,
        offset.to_bytes(8, "big") + token
        + len(name_bytes).to_bytes(2, "big") + name_bytes,
    )

    frame_type, payload_len = H.read_frame_header(sock)
    if frame_type == H.T_ERROR:
        _, message = _read_error_payload(sock, payload_len)
        fail(EXIT_SERVER, message)

    meta = H.read_exact_bytes(sock, H.GET_META)
    total_size = int.from_bytes(meta[:8], "big")
    start = int.from_bytes(meta[8:16], "big")
    digest = meta[16:]
    partial = H.partial_name(dest, digest)

    if start:
        print(f"resuming '{filename}' from byte {start}")
    elif existing:
        #server offered us a different file; our old bytes are useless
        os.remove(existing)

    try:
        with open(partial, "ab" if start else "wb") as f:
            remaining = total_size - start
            while remaining > 0:
                chunk = sock.recv(min(65536, remaining))
                if not chunk:
                    raise ConnectionError("connection lost during download")
                f.write(chunk)
                remaining -= len(chunk)
    except Exception as e:
        #the partial survives on purpose: it is what makes a retry resumable
        fail(EXIT_CONN, f"download interrupted: {e} (partial kept for resume)")

    if H.sha256_file(partial) != digest:
        os.remove(partial)
        fail(EXIT_CONN, "downloaded content did not match the server's hash")

    os.replace(partial, dest)
    print(f"downloaded '{filename}' ({total_size} bytes, hash verified) -> {dest}")


def do_put(sock, filename, remote_name=None):
    if not os.path.isfile(filename):
        fail(EXIT_LOCAL, f"'{filename}' not found locally")
    file_size = os.path.getsize(filename)
    digest = H.sha256_file(filename)
    remote = remote_name or os.path.basename(filename)
    name_bytes = remote.encode("utf-8")

    #step 1: state size and hash; the server replies with the offset it
    #already holds, so no body byte is sent twice
    H.write_frame(
        sock, H.T_PUT,
        file_size.to_bytes(8, "big") + digest
        + len(name_bytes).to_bytes(2, "big") + name_bytes,
    )
    frame_type, payload = H.read_frame(sock)
    if frame_type == H.T_ERROR:
        _, message = H.unpack_error(payload)
        fail(EXIT_SERVER, message)
    resume_offset = int.from_bytes(payload[:8], "big")
    if resume_offset:
        print(f"resuming upload from byte {resume_offset}")

    #step 2: stream the bytes the server is missing, then hear the verdict
    with open(filename, "rb") as f:
        f.seek(resume_offset)
        remaining = file_size - resume_offset
        while remaining > 0:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                break
            sock.sendall(chunk)
            remaining -= len(chunk)

    frame_type, payload = H.read_frame(sock)
    if frame_type == H.T_ERROR:
        _, message = H.unpack_error(payload)
        fail(EXIT_SERVER, message)
    print(f"uploaded '{filename}' ({file_size} bytes, hash verified) -> {remote}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="retriever",
        description="File transfer client speaking the retriever v2 protocol.",
    )
    parser.add_argument("host", help="server address")
    parser.add_argument("port", type=int, help="server port (1024-65535)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="list files on the server")

    get_cmd = commands.add_parser("get", help="download a file")
    get_cmd.add_argument("filename", help="name of the file on the server")
    get_cmd.add_argument("-o", "--output", help="local name to save as")

    put_cmd = commands.add_parser("put", help="upload a file")
    put_cmd.add_argument("filename", help="local file to upload")
    put_cmd.add_argument("remote_name", nargs="?", help="name to store it as")

    args = parser.parse_args(argv)
    if not (1024 <= args.port <= 65535):
        parser.error("port must be between 1024 and 65535")

    sock = connect(args.host, args.port)
    try:
        if args.command == "list":
            do_list(sock)
        elif args.command == "get":
            do_get(sock, args.filename, args.output)
        elif args.command == "put":
            do_put(sock, args.filename, args.remote_name)

        #polite goodbye: QUIT and wait for the server's OK
        H.write_frame(sock, H.T_QUIT)
        H.read_frame(sock)
    except H.FrameError as e:
        fail(EXIT_CONN, f"protocol error: {e}")
    except socket.timeout:
        fail(EXIT_CONN, "server stopped responding")
    except (ConnectionError, OSError) as e:
        fail(EXIT_CONN, f"connection failed: {e}")
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
