# retriever

A file transfer client and server built on raw TCP sockets, with its own binary wire protocol.

Fetches a file and brings it back intact.

> Baseline import: this first commit is the original coursework version, unmodified.
> Everything after it is the rebuild. See `docs/PROTOCOL.md` for the wire format.

## Status

Phase 0 — baseline imported.

## Usage

    python3 server/server.py <port>
    python3 client/client.py <server_ip> <port> <list|get|put> [filename]
