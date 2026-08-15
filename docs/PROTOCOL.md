# Wire protocol

## Version 1 (baseline)

This documents the protocol exactly as the baseline coursework code
implements it, including its flaws. All integers are unsigned, big-endian.
One request per TCP connection: the server closes the socket after each
command.

### Status codes

| code | meaning                                           |
|------|---------------------------------------------------|
| 0    | OK                                                |
| 1    | client error (see below: this code is overloaded) |
| 2    | internal server error                             |

Status 1 is sent for all of: invalid filename, file not found (GET),
extension not .jpg/.jpeg/.png (PUT), body does not start with JPEG or PNG
magic bytes (PUT), file already exists on server (PUT), and unknown
command byte. The client cannot tell these apart.

### Filename rules

The server rejects a filename (status 1) unless all of the following hold:

- not empty, at most 255 characters
- printable ASCII only (0x20 to 0x7E)
- contains no `/`, no `\`, and no `..` substring

### Request: LIST

| offset | size | type  | field   | notes       |
|--------|------|-------|---------|-------------|
| 0      | 1    | u8    | command | always 0x00 |

### Reply: LIST

| offset | size | type  | field       | notes                              |
|--------|------|-------|-------------|------------------------------------|
| 0      | 1    | u8    | status      | see status codes                   |
| 1      | 8    | u64   | payload_len | 0 on failure or empty directory    |
| 9      | var  | bytes | payload     | entry names, UTF-8, 0x00-separated |

The server lists every entry in its working directory, including
subdirectories, with no way to tell files and directories apart.

### Request: GET

| offset | size | type  | field    | notes                 |
|--------|------|-------|----------|-----------------------|
| 0      | 1    | u8    | command  | always 0x01           |
| 1      | 2    | u16   | name_len | bytes, not characters |
| 3      | var  | bytes | name     | UTF-8                 |

### Reply: GET

| offset | size | type  | field     | notes                   |
|--------|------|-------|-----------|-------------------------|
| 0      | 1    | u8    | status    | see status codes        |
| 1      | 8    | u64   | file_size | 0 on failure            |
| 9      | var  | bytes | body      | exactly file_size bytes |

### Request: PUT

| offset | size | type  | field     | notes                   |
|--------|------|-------|-----------|-------------------------|
| 0      | 1    | u8    | command   | always 0x02             |
| 1      | 2    | u16   | name_len  | bytes, not characters   |
| 3      | var  | bytes | name      | UTF-8                   |
| 3+n    | 8    | u64   | file_size | size of body            |
| 11+n   | var  | bytes | body      | exactly file_size bytes |

### Reply: PUT

| offset | size | type | field  | notes                         |
|--------|------|------|--------|-------------------------------|
| 0      | 1    | u8   | status | see status codes, no u64 here |

Unlike LIST and GET, the PUT reply is a single byte. The server also
peeks at the first min(8, file_size) bytes of the body before accepting:
if the name check, extension check, magic-byte check, or an
already-existing file rejects the upload, the server sends the status
byte and closes the connection without reading the rest of the body. A
client mid-upload sees a connection reset rather than a clean error.

An unknown command byte gets a LIST-shaped reply: status 1 plus a u64
zero, even though the server never understood the request.

### Worked example: one full GET exchange

Client fetches `cat.png` (2,048 bytes on the server).

Client sends 10 bytes:

    01                    command = GET
    00 07                 name_len = 7
    63 61 74 2E 70 6E 67  "cat.png"

Server replies with 9 + 2,048 bytes:

    00                          status = OK
    00 00 00 00 00 00 08 00     file_size = 2048
    ...                         2,048 bytes of file body

Same request for a file that does not exist. Server replies with 9 bytes
and closes:

    01                          status = client error
    00 00 00 00 00 00 00 00     file_size = 0

### Known problems with v1

- Status 1 means six different failures; the client cannot report which.
- Reply shapes are inconsistent: LIST/GET replies carry a u64, PUT's is
  a lone byte, and unknown commands get a LIST-shaped reply.
- Early PUT rejection closes the socket without draining the body, so
  the client crashes on connection reset instead of reading the error.
- No magic number or version byte: any stray byte on the port is
  interpreted as a command.
- One command per connection; every request pays full connection setup.
- No integrity checking: a corrupted transfer is undetectable.
- No socket timeouts on either side: a stalled peer hangs forever.

## Version 2

Version 2 replaces v1's bare command bytes with framed, versioned
messages and structured errors. All integers remain unsigned,
big-endian. One command per connection (sessions arrive in a later
version). v1 is no longer accepted: its first byte fails the magic
check and the server answers with an UNSUPPORTED_VERSION error frame.

### Layering notes

retriever is an application protocol on TCP. TCP already provides
ordered, reliable, flow-controlled delivery of a byte stream, so v2
does not re-implement any of that. What TCP does not provide, and v2
therefore adds, is: message boundaries (the frame header's length
field), protocol identification and versioning (magic + version), and
application-level errors (error frames). End-to-end integrity checking
of file contents is deliberately deferred to a later version.

### Frame header

Every message in both directions begins with a fixed 14-byte header:

| offset | size | type | field       | notes                        |
|--------|------|------|-------------|------------------------------|
| 0      | 4    | bytes| magic       | "RTRV" = 52 54 52 56         |
| 4      | 1    | u8   | version     | always 0x02                  |
| 5      | 1    | u8   | type        | see frame types              |
| 6      | 8    | u64  | payload_len | bytes following the header   |

A receiver that sees wrong magic or an unsupported version must send
an UNSUPPORTED_VERSION error frame and close (wrong magic means the
peer is not speaking retriever at all, which is the same category of
incompatibility). A payload
longer than the cap for its frame type is MALFORMED.

### Frame types

| type | direction        | meaning | payload cap |
|------|------------------|---------|-------------|
| 0x10 | client to server | HELLO   | 0           |
| 0x00 | client to server | LIST    | 0           |
| 0x01 | client to server | GET     | 64 KiB      |
| 0x02 | client to server | PUT     | 64 KiB      |
| 0x80 | server to client | OK      | 64 KiB, except GET replies (see GET) |
| 0x81 | server to client | ERROR   | 64 KiB      |

Unknown frame types get a MALFORMED error.

### Error frames (type 0x81)

| offset | size | type  | field   | notes                         |
|--------|------|-------|---------|-------------------------------|
| 0      | 1    | u8    | reason  | see reason codes              |
| 1      | 2    | u16   | msg_len | may be 0                      |
| 3      | var  | bytes | message | UTF-8, human-readable detail  |

Reason codes:

| code | name                | meaning                                  |
|------|---------------------|------------------------------------------|
| 1    | BAD_NAME            | filename fails the filename rules        |
| 2    | NOT_FOUND           | GET target does not exist                |
| 3    | ALREADY_EXISTS      | PUT target already exists                |
| 4    | MALFORMED           | frame violates this spec                 |
| 5    | UNSUPPORTED_VERSION | bad magic or version, incl. v1 peers     |
| 6    | INTERNAL            | server-side failure                      |
| 7    | TOO_LARGE           | PUT file_size exceeds the server's limit |

### Connection lifecycle

    client                          server
      | -- HELLO ------------------> |
      | <------------------- OK --- |      (or ERROR + close)
      | -- LIST / GET / PUT -------> |
      |        ... command exchange ...
      |         (connection closes)

HELLO carries no payload; the header's version byte is the handshake.
Any command frame sent before HELLO is MALFORMED.

### LIST

Request: LIST frame, empty payload.
Reply: OK frame; payload is entry names, UTF-8, separated by 0x00
(same format as v1), or empty for an empty directory.

### GET

Request: GET frame; payload is u16 name_len + name bytes.
Reply: ERROR frame, or an OK frame whose payload is the file body
(payload_len = file size). The body is the one payload exempt from the
64 KiB cap: the receiver streams it to disk and must not buffer it in
memory.

### PUT (two-step)

Step 1. Request: PUT frame; payload is u16 name_len + name bytes +
u64 file_size. The server validates the name, the target, and the
size (a file_size above the server's limit, 1 GiB by default, is
rejected TOO_LARGE) and replies with an empty OK frame (permission to
send) or an ERROR frame. Nothing is written to disk yet, so a rejected
PUT costs bytes, not bandwidth.

Step 2. On OK, the client sends exactly file_size raw bytes: no frame
around the body, because TCP already carries a length-known byte
stream and the size was stated in step 1. The server then replies with
a final OK (file stored) or ERROR frame.

Filename extension and magic-byte sniffing no longer reject uploads;
a mismatch is logged as a warning server-side. v2 is a file server,
not an image server.

### Filename rules

Unchanged from v1: 1 to 255 printable ASCII characters (0x20 to 0x7E),
no `/`, no `\`, no `..` substring.

### Worked example: HELLO + GET, byte by byte

Client connects and sends HELLO (14 bytes):

    52 54 52 56              magic "RTRV"
    02                       version 2
    10                       type HELLO
    00 00 00 00 00 00 00 00  payload_len 0

Server replies OK (14 bytes):

    52 54 52 56 02 80 00 00 00 00 00 00 00 00

Client requests cat.png (14 + 9 bytes):

    52 54 52 56 02 01        header, type GET
    00 00 00 00 00 00 00 09  payload_len 9
    00 07                    name_len 7
    63 61 74 2E 70 6E 67     "cat.png"

Server replies with the file (14 + 2048 bytes):

    52 54 52 56 02 80        header, type OK
    00 00 00 00 00 00 08 00  payload_len 2048
    ...                      2048 bytes of file body

Failure case, cat.png missing (14 + 12 bytes):

    52 54 52 56 02 81        header, type ERROR
    00 00 00 00 00 00 00 0C  payload_len 12
    02                       reason NOT_FOUND
    00 09                    msg_len 9
    6E 6F 74 20 66 6F 75 6E 64   "not found"

### v1 problems resolved and remaining

Resolved by v2: overloaded status codes, inconsistent reply shapes,
close-without-drain on PUT rejection (structurally impossible with the
two-step flow), stray bytes treated as commands, undetectable version
mismatch.

Still open, by design: one command per connection (sessions), no
integrity checking (both deferred to later versions).
