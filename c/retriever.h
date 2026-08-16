/*
 * retriever.h - the v3 wire protocol, in C.
 *
 * This is a second implementation of docs/PROTOCOL.md. It talks to the
 * same Python server, so the two must agree byte for byte.
 *
 * Notes for a reader coming from Python:
 *
 *  - C has no exceptions. Every function that can fail returns an int:
 *    0 for success, -1 for failure, and it prints the reason itself.
 *
 *  - C arrays do not know their own length. Wherever Python would pass
 *    a bytes object, C passes two things: a pointer to the first byte
 *    and a count. Losing track of that count is how buffer overflows
 *    happen, so the count travels with the pointer everywhere below.
 *
 *  - There is no int.to_bytes(). The big-endian packing that Python
 *    gets from the standard library is written out by hand here, one
 *    shift and mask per byte.
 *
 * Build with the supplied Makefile: it sets _POSIX_C_SOURCE, without
 * which the socket functions are invisible on glibc under -std=c11.
 */
#ifndef RETRIEVER_H
#define RETRIEVER_H

#include <stddef.h>
#include <stdint.h>

/* --- protocol constants (must match docs/PROTOCOL.md) ------------------ */

#define RTRV_MAGIC       "RTRV"
#define RTRV_MAGIC_LEN   4
#define RTRV_VERSION     3
#define RTRV_HEADER_SIZE 14  /* 4 magic + 1 version + 1 type + 8 length */

/* frame types */
#define RTRV_T_LIST  0x00
#define RTRV_T_GET   0x01
#define RTRV_T_PUT   0x02
#define RTRV_T_HELLO 0x10
#define RTRV_T_QUIT  0x11
#define RTRV_T_OK    0x80
#define RTRV_T_ERROR 0x81

/* error reason codes */
#define RTRV_E_BAD_NAME            1
#define RTRV_E_NOT_FOUND           2
#define RTRV_E_ALREADY_EXISTS      3
#define RTRV_E_MALFORMED           4
#define RTRV_E_UNSUPPORTED_VERSION 5
#define RTRV_E_INTERNAL            6
#define RTRV_E_TOO_LARGE           7
#define RTRV_E_CORRUPT             8

/* the largest payload we are willing to hold in memory at once */
#define RTRV_PAYLOAD_CAP (64 * 1024)

#define RTRV_TOKEN_SIZE  8    /* resume token: first 8 bytes of the hash */
#define RTRV_GET_META    48   /* u64 total_size, u64 start_offset, 32-byte hash */
#define RTRV_MAX_NAME    255  /* protocol limit on a filename */
#define RTRV_PATH_MAX    1024 /* our own limit on a local path */

/* --- byte packing ------------------------------------------------------ */

/*
 * Write a big-endian integer into a buffer, most significant byte first.
 * The caller must have already checked the buffer has room: these
 * functions cannot know how big it is.
 */
void rtrv_put_u16(unsigned char *dst, uint16_t value);
void rtrv_put_u64(unsigned char *dst, uint64_t value);

/* Read a big-endian integer back out of a buffer. */
uint16_t rtrv_get_u16(const unsigned char *src);
uint64_t rtrv_get_u64(const unsigned char *src);

/* --- socket helpers ---------------------------------------------------- */

/*
 * Read exactly n bytes, looping until they have all arrived.
 *
 * One recv() call can return fewer bytes than asked for: TCP is a byte
 * stream, not a message queue, and the kernel hands over whatever has
 * turned up so far. This loop is what turns that stream back into the
 * fixed-size reads the protocol is written in terms of.
 *
 * Returns 0 on success, -1 if the peer closed early or the read failed.
 */
int rtrv_read_exact(int fd, void *buf, size_t n);

/* Write all n bytes, looping for the same reason in reverse. */
int rtrv_write_all(int fd, const void *buf, size_t n);

/* Connect to host:port over TCP. Returns a socket fd, or -1. */
int rtrv_connect(const char *host, const char *port);

/* --- frames ------------------------------------------------------------ */

/* Send one frame: the 14-byte header, then payload_len bytes of payload. */
int rtrv_write_frame(int fd, uint8_t type, const void *payload,
                     uint64_t payload_len);

/*
 * Read and validate a frame header. Fills in type and payload_len, and
 * does not touch the payload, so a caller streaming a large body can
 * handle it itself.
 */
int rtrv_read_frame_header(int fd, uint8_t *type, uint64_t *payload_len);

/*
 * Read a whole frame whose payload fits in memory.
 *
 * On success *payload points at a malloc'd buffer that the CALLER must
 * free(). C has no garbage collector: whoever allocates decides who
 * frees, and saying so in the comment is half of how that stays true.
 * A zero-length payload sets *payload to NULL.
 */
int rtrv_read_frame(int fd, uint8_t *type, unsigned char **payload,
                    uint64_t *payload_len);

/*
 * Print a human-readable message for an ERROR frame payload.
 * Layout is u8 reason, u16 message length, then the message bytes.
 */
void rtrv_report_error(const unsigned char *payload, uint64_t payload_len);

/* --- transfers ---------------------------------------------------------- */

/* Split a path into directory and last component. */
void rtrv_split_path(const char *path, char *dir, size_t dir_size,
                     char *base, size_t base_size);

/* Build the partial file name for a transfer: .<name>.<token hex>.part */
void rtrv_partial_name(const char *dest, const unsigned char *digest,
                       char *out, size_t out_size);

/*
 * Find an existing partial for dest. Returns 1 if exactly one was found
 * (filling in path, size and token), 0 if none, -1 on error.
 */
int rtrv_find_partial(const char *dest, char *path_out, size_t path_size,
                      uint64_t *size_out, unsigned char *token_out);

/* Download remote_name into dest, resuming and verifying. Returns 0 or -1. */
int rtrv_do_get(int fd, const char *remote_name, const char *dest);

/* Upload local_path as remote_name, resuming and verifying. Returns 0 or -1. */
int rtrv_do_put(int fd, const char *local_path, const char *remote_name);

#endif /* RETRIEVER_H */
