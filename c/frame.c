/*
 * frame.c - byte packing, exact reads and writes, and v3 frames.
 *
 * This is the file the Python protocol.py turns into when you take away
 * the standard library.
 */
#include "retriever.h"

#include <errno.h>
#include <netdb.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

/* --- byte packing ------------------------------------------------------ */

/*
 * Big-endian means most significant byte first, which is what the spec
 * says and also the traditional order on a network. Python writes this
 * as value.to_bytes(2, "big"); in C it is a shift and a mask per byte.
 *
 * The & 0xFF is not strictly needed when assigning to an unsigned char
 * (the conversion truncates anyway), but writing it makes the intent
 * obvious: take the low eight bits of what is left.
 */
void rtrv_put_u16(unsigned char *dst, uint16_t value)
{
    dst[0] = (unsigned char)((value >> 8) & 0xFF);
    dst[1] = (unsigned char)(value & 0xFF);
}

void rtrv_put_u64(unsigned char *dst, uint64_t value)
{
    for (int i = 0; i < 8; i++) {
        /* byte 0 holds the highest 8 bits, byte 7 the lowest */
        dst[i] = (unsigned char)((value >> (56 - 8 * i)) & 0xFF);
    }
}

uint16_t rtrv_get_u16(const unsigned char *src)
{
    return (uint16_t)((uint16_t)src[0] << 8 | (uint16_t)src[1]);
}

uint64_t rtrv_get_u64(const unsigned char *src)
{
    uint64_t value = 0;
    for (int i = 0; i < 8; i++) {
        /* shift what we have up by a byte, then drop the next one in */
        value = (value << 8) | (uint64_t)src[i];
    }
    return value;
}

/* --- socket helpers ---------------------------------------------------- */

int rtrv_read_exact(int fd, void *buf, size_t n)
{
    /*
     * unsigned char* because we do pointer arithmetic below: adding to a
     * void* is not standard C, and we want to advance exactly one byte
     * at a time.
     */
    unsigned char *p = buf;
    size_t got = 0;

    while (got < n) {
        ssize_t r = recv(fd, p + got, n - got, 0);
        if (r < 0) {
            /* EINTR means a signal interrupted the call, not a failure */
            if (errno == EINTR) {
                continue;
            }
            fprintf(stderr, "error: read failed: %s\n", strerror(errno));
            return -1;
        }
        if (r == 0) {
            fprintf(stderr, "error: connection closed early\n");
            return -1;
        }
        got += (size_t)r;
    }
    return 0;
}

int rtrv_write_all(int fd, const void *buf, size_t n)
{
    const unsigned char *p = buf;
    size_t sent = 0;

    while (sent < n) {
        ssize_t w = send(fd, p + sent, n - sent, 0);
        if (w < 0) {
            if (errno == EINTR) {
                continue;
            }
            fprintf(stderr, "error: write failed: %s\n", strerror(errno));
            return -1;
        }
        sent += (size_t)w;
    }
    return 0;
}

int rtrv_connect(const char *host, const char *port)
{
    struct addrinfo hints;
    struct addrinfo *results = NULL;

    /*
     * memset first: struct fields are not zero-initialised in C, and
     * getaddrinfo reads every one of them.
     */
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;        /* IPv4, matching the Python client */
    hints.ai_socktype = SOCK_STREAM;  /* TCP */

    int rc = getaddrinfo(host, port, &hints, &results);
    if (rc != 0) {
        fprintf(stderr, "error: cannot resolve %s:%s: %s\n",
                host, port, gai_strerror(rc));
        return -1;
    }

    int fd = -1;
    for (struct addrinfo *a = results; a != NULL; a = a->ai_next) {
        fd = socket(a->ai_family, a->ai_socktype, a->ai_protocol);
        if (fd < 0) {
            continue;
        }
        if (connect(fd, a->ai_addr, a->ai_addrlen) == 0) {
            break;  /* connected */
        }
        close(fd);
        fd = -1;
    }
    /* getaddrinfo allocated that list, so we release it either way */
    freeaddrinfo(results);

    if (fd < 0) {
        fprintf(stderr, "error: cannot connect to %s:%s: %s\n",
                host, port, strerror(errno));
        return -1;
    }
    return fd;
}

/* --- frames ------------------------------------------------------------ */

int rtrv_write_frame(int fd, uint8_t type, const void *payload,
                     uint64_t payload_len)
{
    unsigned char header[RTRV_HEADER_SIZE];

    memcpy(header, RTRV_MAGIC, RTRV_MAGIC_LEN);
    header[4] = RTRV_VERSION;
    header[5] = type;
    rtrv_put_u64(header + 6, payload_len);

    if (rtrv_write_all(fd, header, sizeof(header)) < 0) {
        return -1;
    }
    if (payload_len > 0 && rtrv_write_all(fd, payload, (size_t)payload_len) < 0) {
        return -1;
    }
    return 0;
}

int rtrv_read_frame_header(int fd, uint8_t *type, uint64_t *payload_len)
{
    unsigned char header[RTRV_HEADER_SIZE];

    if (rtrv_read_exact(fd, header, sizeof(header)) < 0) {
        return -1;
    }
    if (memcmp(header, RTRV_MAGIC, RTRV_MAGIC_LEN) != 0) {
        fprintf(stderr, "error: bad magic: not a retriever server\n");
        return -1;
    }
    if (header[4] != RTRV_VERSION) {
        fprintf(stderr, "error: server speaks protocol v%u, this client speaks v%d\n",
                (unsigned)header[4], RTRV_VERSION);
        return -1;
    }
    *type = header[5];
    *payload_len = rtrv_get_u64(header + 6);
    return 0;
}

int rtrv_read_frame(int fd, uint8_t *type, unsigned char **payload,
                    uint64_t *payload_len)
{
    *payload = NULL;

    if (rtrv_read_frame_header(fd, type, payload_len) < 0) {
        return -1;
    }
    if (*payload_len > RTRV_PAYLOAD_CAP) {
        fprintf(stderr, "error: server sent an oversized payload (%llu bytes)\n",
                (unsigned long long)*payload_len);
        return -1;
    }
    if (*payload_len == 0) {
        return 0;  /* nothing to read; *payload stays NULL */
    }

    unsigned char *buf = malloc((size_t)*payload_len);
    if (buf == NULL) {
        /* malloc returns NULL when it cannot allocate. Python raises
         * MemoryError for you; here, checking is the caller's job. */
        fprintf(stderr, "error: out of memory\n");
        return -1;
    }
    if (rtrv_read_exact(fd, buf, (size_t)*payload_len) < 0) {
        free(buf);  /* do not leak the buffer on the failure path */
        return -1;
    }
    *payload = buf;
    return 0;
}

void rtrv_report_error(const unsigned char *payload, uint64_t payload_len)
{
    if (payload == NULL || payload_len < 3) {
        fprintf(stderr, "error: server sent a malformed error frame\n");
        return;
    }
    unsigned reason = payload[0];
    uint16_t message_len = rtrv_get_u16(payload + 1);

    if ((uint64_t)message_len + 3 > payload_len) {
        fprintf(stderr, "error: error frame message length is wrong\n");
        return;
    }
    /*
     * The message is NOT null-terminated on the wire: it is length-
     * prefixed, so printing it with %s would run off the end. "%.*s"
     * takes the length as an argument and prints exactly that many.
     */
    fprintf(stderr, "error: %.*s (reason %u)\n",
            (int)message_len, (const char *)payload + 3, reason);
}
