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
