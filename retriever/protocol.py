import socket
import os
import glob
import time
import hashlib

#seconds either side waits on a silent peer before giving up
SOCKET_TIMEOUT = 30

#---------------------------------------------------------------------------
#v2 framing (docs/PROTOCOL.md "Version 2")
#---------------------------------------------------------------------------

MAGIC = b"RTRV"
VERSION = 3
HEADER_SIZE = 14  #4 magic + 1 version + 1 type + 8 payload_len

HASH_SIZE = 32    #SHA-256
TOKEN_SIZE = 8    #resume token: first 8 bytes of the hash

#fixed-size prefixes of the v3 payloads (see docs/PROTOCOL.md)
GET_PREFIX = 8 + TOKEN_SIZE + 2      #offset, token, name_len
GET_META = 8 + 8 + HASH_SIZE         #total_size, start_offset, hash
PUT_PREFIX = 8 + HASH_SIZE + 2       #file_size, hash, name_len

#partial files older than this are disposable
PARTIAL_MAX_AGE = 24 * 3600

#frame types
T_LIST = 0x00
T_GET = 0x01
T_PUT = 0x02
T_HELLO = 0x10
T_QUIT = 0x11
T_OK = 0x80
T_ERROR = 0x81

#error reason codes
E_BAD_NAME = 1
E_NOT_FOUND = 2
E_ALREADY_EXISTS = 3
E_MALFORMED = 4
E_UNSUPPORTED_VERSION = 5
E_INTERNAL = 6
E_TOO_LARGE = 7
E_CORRUPT = 8

#largest file_size a PUT may claim; guards the server's disk
MAX_FILE_SIZE = 1024 ** 3   #1 GiB

#max payload per frame type; None means the caller streams the payload
#itself (GET reply bodies) and must not read it through read_frame
PAYLOAD_CAPS = {
    T_HELLO: 0,
    T_QUIT: 0,
    T_LIST: 0,
    T_GET: 64 * 1024,
    T_PUT: 64 * 1024,
    T_OK: 64 * 1024,
    T_ERROR: 64 * 1024,
}


class FrameError(Exception):
    """The peer violated the v2 framing spec. Carries the reason code
    the other side should be told about."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def write_frame_header(sock, frame_type, payload_len):
    """Send just a v2 header; the caller streams payload_len bytes itself.
    Used for GET reply bodies, which go disk-to-socket without buffering."""
    sock.sendall(MAGIC + bytes([VERSION, frame_type]) + payload_len.to_bytes(8, "big"))


def write_frame(sock, frame_type, payload=b""):
    """Send one v2 frame: 14-byte header followed by the payload."""
    header = MAGIC + bytes([VERSION, frame_type]) + len(payload).to_bytes(8, "big")
    sock.sendall(header + payload)


def read_frame_header(sock):
    """Read and validate a 14-byte v2 header. Returns (frame_type, payload_len).

    Raises FrameError on wrong magic/version, unknown type, or a payload
    length over the cap for that type. Does not read the payload, so GET
    reply bodies can be streamed by the caller.
    """
    header = read_exact_bytes(sock, HEADER_SIZE)
    if header[:4] != MAGIC:
        raise FrameError(E_UNSUPPORTED_VERSION, "bad magic: not a retriever v2 peer")
    if header[4] != VERSION:
        raise FrameError(E_UNSUPPORTED_VERSION, f"unsupported version {header[4]}")
    frame_type = header[5]
    if frame_type not in PAYLOAD_CAPS:
        raise FrameError(E_MALFORMED, f"unknown frame type 0x{frame_type:02X}")
    payload_len = int.from_bytes(header[6:14], "big")
    return frame_type, payload_len


def read_frame(sock):
    """Read one complete capped frame. Returns (frame_type, payload).

    Enforces the per-type payload cap, so never use this to receive a GET
    reply body; use read_frame_header and stream instead.
    """
    frame_type, payload_len = read_frame_header(sock)
    if payload_len > PAYLOAD_CAPS[frame_type]:
        raise FrameError(
            E_MALFORMED,
            f"payload of {payload_len} bytes exceeds cap for type 0x{frame_type:02X}",
        )
    payload = read_exact_bytes(sock, payload_len) if payload_len else b""
    return frame_type, payload


def pack_error(reason, message=""):
    """Build an ERROR frame payload: u8 reason + u16 msg_len + UTF-8 text."""
    msg = message.encode("utf-8")
    return bytes([reason]) + len(msg).to_bytes(2, "big") + msg


def unpack_error(payload):
    """Parse an ERROR frame payload. Returns (reason, message)."""
    if len(payload) < 3:
        raise FrameError(E_MALFORMED, "error payload shorter than 3 bytes")
    reason = payload[0]
    msg_len = int.from_bytes(payload[1:3], "big")
    if len(payload) != 3 + msg_len:
        raise FrameError(E_MALFORMED, "error payload length does not match msg_len")
    return reason, payload[3:].decode("utf-8")


def read_exact_bytes(sock,n):
    """read exactly n bytes from socket,
    keeps reading until it has all n bytes"""
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n-len(data))
        if not chunk:
            raise ConnectionError("Connection closed early")
        data += chunk
    return bytes(data)


def send_u8(sock,value):
    """ sends a single unsigned 1 byte( 8 bits)  integer, 
    takes socket and numeric label (value) )"""
    #makes sure its 1 byte only
    if not (0 <= value <= 255):
        raise ValueError("send_u8: value must be between 0 and 255")
    sock.sendall(value.to_bytes(1, "big"))  #converts integer to 1 byte, "big" refers to it being big-endian

def recv_u8(sock):
    """Recives a 1 byte integer from the socket and return it as an int"""
    return int.from_bytes(read_exact_bytes(sock,1),"big")

def send_u16(sock, value):
    """sends an unsigned 2 bytes (16 bit)  integer through the socket"""
    if not ( 0 <= value <= 65535):
        raise ValueError("send_u16: value must be between 0 and 65,535")
    sock.sendall(value.to_bytes(2, "big"))

def recv_u16(sock):
    """Recive an unsigned 2 bytes integer from the socket and return it as an int"""
    return int.from_bytes(read_exact_bytes(sock,2),"big")

def send_u64(sock,value):
    """send an unsigned 8 bytes (64 bits) integer through the socket """
    if not (0 <= value <= 18446744073709551615):
        raise ValueError ("send_u64 : value must be between 0 and 18,446,744,073,709,551,615")
    sock.sendall(value.to_bytes(8,"big"))

def recv_u64(sock):
    """Recives an 8 byte unsigned integer from the socket and return it"""
    return int.from_bytes(read_exact_bytes(sock,8),"big")

#---------------------------------------------------------------------------
#v3 integrity and resume helpers
#---------------------------------------------------------------------------

def sha256_file(path):
    """SHA-256 of a whole file, read in chunks so size does not matter."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.digest()


def partial_name(name, digest):
    """Partial file name for a transfer: .<name>.<token hex>.part

    The resume token lives in the filename, so a resume of a *different*
    file cannot find these bytes and will start fresh instead of
    appending onto foreign data.
    """
    directory, base = os.path.split(name)
    return os.path.join(directory, ".{}.{}.part".format(base, digest[:TOKEN_SIZE].hex()))


def partial_token(path):
    """Recover the resume token from a partial file's name, or None."""
    parts = os.path.basename(path).split(".")
    if len(parts) < 4 or parts[-1] != "part":
        return None
    try:
        token = bytes.fromhex(parts[-2])
    except ValueError:
        return None
    return token if len(token) == TOKEN_SIZE else None


def find_partial(name):
    """Find an existing partial file for this target name, or None."""
    directory, base = os.path.split(name)
    pattern = os.path.join(directory, ".{}.*.part".format(glob.escape(base)))
    matches = glob.glob(pattern)
    return matches[0] if len(matches) == 1 else None


def sweep_partials(max_age=PARTIAL_MAX_AGE):
    """Delete abandoned partial files in the current directory.

    Called at server startup: a restart is the natural moment to tidy,
    and it needs no background thread racing live uploads.
    """
    removed = 0
    now = time.time()
    for path in glob.glob(".*.part"):
        try:
            if now - os.path.getmtime(path) > max_age:
                os.remove(path)
                removed += 1
        except OSError:
            pass   #vanished under us, or not ours to remove
    return removed


def check_filename(name):
    """return true if filename is valid and safe"""
    #if no filename or if it is longer than 255 (most OS cant handle over 255 characters filename) return false
    if not name or len(name) > 255:
        return False
    #making sure no directory is allowed
    if "/" in name or "\\" in name or ".." in name:
        return False
    #convert filename characters into ASCII code
    #loops through each character in filename and checks its ASCII code
    return all(32 <= ord(ch) < 127 for ch in name)

def is_image(name):
    """checks if a filename ends with jpg / jpeg / png """
    n = name.lower()
    return n.endswith(".jpg") or n.endswith(".jpeg") or n.endswith(".png")

def is_jpeg_header(header):
    return len(header) >=2 and header[:2]==b"\xFF\xD8"

#same idea but with png 
def is_png_header(header):
    return len(header) >=8 and header[:8]== b"\x89PNG\r\n\x1a\n"
