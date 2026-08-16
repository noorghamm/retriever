/*
 * sha256.h - SHA-256, implemented from FIPS 180-4.
 *
 * Python hands you hashlib.sha256(). C hands you nothing, so this is
 * the algorithm written out: a compression function over 64-byte
 * blocks, plus the padding rules that turn a message of any length
 * into a whole number of blocks.
 *
 * The API is streaming (init, update repeatedly, final) because a
 * 100 MB file must be hashed as it is read, never held in memory.
 */
#ifndef SHA256_H
#define SHA256_H

#include <stddef.h>
#include <stdint.h>

#define SHA256_DIGEST_SIZE 32
#define SHA256_BLOCK_SIZE  64

typedef struct {
    uint32_t state[8];                        /* the running hash */
    uint64_t bit_count;                       /* message length in bits */
    unsigned char buffer[SHA256_BLOCK_SIZE];  /* partial block not yet processed */
    size_t buffered;                          /* bytes currently in buffer */
} sha256_ctx;

void sha256_init(sha256_ctx *ctx);
void sha256_update(sha256_ctx *ctx, const void *data, size_t len);
void sha256_final(sha256_ctx *ctx, unsigned char digest[SHA256_DIGEST_SIZE]);

/* Hash a whole file. Returns 0, or -1 if the file cannot be read. */
int sha256_file(const char *path, unsigned char digest[SHA256_DIGEST_SIZE]);

/* Write digest as 2*len lowercase hex chars plus a terminating zero. */
void sha256_hex(const unsigned char *digest, size_t len, char *out);

#endif /* SHA256_H */
