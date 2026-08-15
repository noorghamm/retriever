# Reading retriever v2 off the wire

`get-exchange.pcap` in this directory is a tcpdump capture of one real
exchange: `retriever.client 127.0.0.1 5050 get LICENSE` against the
live server, recorded on the loopback interface.

Reproduce it:

    sudo tcpdump -i lo0 -w docs/get-exchange.pcap 'tcp port 5050'
    # in another terminal: run the server, run the client
    tcpdump -r docs/get-exchange.pcap -nn -X

## What the capture shows, packet by packet

Because this is loopback, there is no ARP (no Ethernet layer) and no
DNS (127.0.0.1 is already an address). The conversation opens with
pure TCP.

### 1-3: the TCP three-way handshake

    Flags [S]   seq 1919979661            client proposes, random ISN
    Flags [S.]  seq 816717188, ack ...662 server accepts, acks ISN+1
    Flags [.]   ack 1                     client confirms

No application byte can travel before this completes. The `[.]`
ack-only packets sprinkled through the rest of the capture are TCP
acknowledging receipt; the application never sees them.

### 4: HELLO (client to server, 14 bytes of data)

    5254 5256 0210 0000 0000 0000 0000
    R T  R V  |  |  \______________/
    magic     v2 HELLO   payload_len 0

The ASCII gutter of tcpdump literally shows `RTRV`. This is the v2
version check: the header itself is the handshake.

### 5: OK (server to client)

    5254 5256 0280 0000 0000 0000 0000
                 ^ type 0x80 = OK: version accepted

### 6: GET request (client to server, 23 bytes)

    5254 5256 0201 0000 0000 0000 0009 0007 4c49 4345 4e53 45
              |  | \_______________/ \__/ \_________________/
         v2  GET    payload_len 9   name_len 7   "LICENSE"

Nested lengths: the frame says 9 payload bytes follow; inside the
payload, the u16 says 7 of them are the name.

### 7: OK header, then the file body

    5254 5256 0280 0000 0000 0000 0430
                                  \__/
                        payload_len 0x0430 = 1072

0x0430 = 4x256 + 3x16 = 1072: the exact size of LICENSE, and the
number the client printed on success. The server sends this header
alone (write_frame_header), then streams the 1072 body bytes from
disk; they arrive in the following packets, and the ASCII gutter of
the first one begins "MIT License".

### The close

FIN/ACK exchanges tear the connection down, one command per
connection, as the v2 spec requires.

## The layering picture

Every packet above is envelopes inside envelopes:

    | IP header | TCP header          | retriever frame  | payload  |
    | addresses | seq/ack, ports,     | magic, version,  | names,   |
    |           | flags, window       | type, length     | file...  |

TCP's numbers (seq/ack) live in the TCP header and never appear inside
the retriever frame; retriever's lengths live in its own header and
mean nothing to TCP. Each layer reads only its own header and treats
everything after it as opaque cargo. That boundary is why the client
and server code never mention sequence numbers, and why TCP never
needs to understand what RTRV means.
