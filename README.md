# retriever

[![tests](https://github.com/noorghamm/retriever/actions/workflows/tests.yml/badge.svg)](https://github.com/noorghamm/retriever/actions/workflows/tests.yml)

A file transfer client and server built on raw TCP sockets, with its own binary wire protocol.

Fetches a file and brings it back intact.

Started as university coursework; the first commit is that baseline, unmodified.
Everything after it is the rebuild. The wire format is documented in
[docs/PROTOCOL.md](docs/PROTOCOL.md).

## Status

- [x] Phase 0: own repo, protocol documented, test harness, baseline bugs fixed
- [ ] Phase 1: framed protocol v2 with versioning and real error codes
- [ ] Phase 2: concurrent server, multi-command sessions
- [ ] Phase 3: resumable transfers, SHA-256 integrity
- [ ] Phase 4: C client speaking the same protocol

## Usage

Run from the repository root:

    python3 -m retriever.server <port>
    python3 -m retriever.client <server_ip> <port> <list|get|put> [filename]

## Tests

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e . pytest
    python -m pytest

The integration tests boot the real server on an ephemeral port and speak the
raw wire protocol at it, including the hostile cases: uploads that die
mid-transfer, downloads cut short by the peer, and clients that connect and
send nothing.
