/*
 * sha256.c - SHA-256 from FIPS 180-4.
 *
 * How the algorithm works, in short:
 *
 *   1. The message is padded to a multiple of 64 bytes: append one 0x80
 *      byte, then zeros, then the original length in bits as a 64-bit
 *      big-endian number. The length goes in at the end so that two
 *      different messages can never pad into the same block sequence.
 *
 *   2. Each 64-byte block is split into sixteen 32-bit big-endian words,
 *      which are expanded into sixty-four words (the "message schedule").
 *
 *   3. Eight working variables are stirred through 64 rounds of the
 *      compression function, then added back into the running hash.
 *
 * All arithmetic is on uint32_t, and it is meant to wrap around: that is
 * defined behaviour for unsigned types in C, and the algorithm depends
 * on it. (For signed types overflow would be undefined behaviour, which
 * is one reason every variable here is unsigned.)
 */
#include "sha256.h"

#include <stdio.h>
#include <string.h>

/*
 * Rotate right: bits pushed off the bottom reappear at the top. C has
 * no rotate operator, so it is two shifts and an or. Note n is never 0
 * here; a rotate by 0 would shift by 32, which is undefined behaviour.
 */
#define ROTR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

#define CH(x, y, z)  (((x) & (y)) ^ (~(x) & (z)))
#define MAJ(x, y, z) (((x) & (y)) ^ ((x) & (z)) ^ ((y) & (z)))
#define BSIG0(x)     (ROTR(x, 2) ^ ROTR(x, 13) ^ ROTR(x, 22))
#define BSIG1(x)     (ROTR(x, 6) ^ ROTR(x, 11) ^ ROTR(x, 25))
#define SSIG0(x)     (ROTR(x, 7) ^ ROTR(x, 18) ^ ((x) >> 3))
#define SSIG1(x)     (ROTR(x, 17) ^ ROTR(x, 19) ^ ((x) >> 10))

/* First 32 bits of the fractional parts of the cube roots of the first
 * 64 primes. These are constants from the standard, not magic numbers
 * anybody invented. */
static const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
    0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
};

/* Process exactly one 64-byte block into ctx->state. */
static void sha256_block(sha256_ctx *ctx, const unsigned char *block)
{
    uint32_t w[64];

    /* the first sixteen words are the block itself, big-endian */
    for (int i = 0; i < 16; i++) {
        w[i] = ((uint32_t)block[i * 4] << 24)
             | ((uint32_t)block[i * 4 + 1] << 16)
             | ((uint32_t)block[i * 4 + 2] << 8)
             | ((uint32_t)block[i * 4 + 3]);
    }
    /* the remaining forty-eight are derived from the earlier ones */
    for (int i = 16; i < 64; i++) {
        w[i] = SSIG1(w[i - 2]) + w[i - 7] + SSIG0(w[i - 15]) + w[i - 16];
    }

    uint32_t a = ctx->state[0], b = ctx->state[1];
    uint32_t c = ctx->state[2], d = ctx->state[3];
    uint32_t e = ctx->state[4], f = ctx->state[5];
    uint32_t g = ctx->state[6], h = ctx->state[7];

    for (int i = 0; i < 64; i++) {
        uint32_t t1 = h + BSIG1(e) + CH(e, f, g) + K[i] + w[i];
        uint32_t t2 = BSIG0(a) + MAJ(a, b, c);
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }

    ctx->state[0] += a; ctx->state[1] += b;
    ctx->state[2] += c; ctx->state[3] += d;
    ctx->state[4] += e; ctx->state[5] += f;
    ctx->state[6] += g; ctx->state[7] += h;
}

void sha256_init(sha256_ctx *ctx)
{
    /* first 32 bits of the fractional parts of the square roots of the
     * first eight primes */
    ctx->state[0] = 0x6a09e667;
    ctx->state[1] = 0xbb67ae85;
    ctx->state[2] = 0x3c6ef372;
    ctx->state[3] = 0xa54ff53a;
    ctx->state[4] = 0x510e527f;
    ctx->state[5] = 0x9b05688c;
    ctx->state[6] = 0x1f83d9ab;
    ctx->state[7] = 0x5be0cd19;
    ctx->bit_count = 0;
    ctx->buffered = 0;
}

void sha256_update(sha256_ctx *ctx, const void *data, size_t len)
{
    const unsigned char *p = data;

    ctx->bit_count += (uint64_t)len * 8;

    /* top up a partial block left over from last time */
    if (ctx->buffered > 0) {
        size_t need = SHA256_BLOCK_SIZE - ctx->buffered;
        size_t take = (len < need) ? len : need;
        memcpy(ctx->buffer + ctx->buffered, p, take);
        ctx->buffered += take;
        p += take;
        len -= take;
        if (ctx->buffered == SHA256_BLOCK_SIZE) {
            sha256_block(ctx, ctx->buffer);
            ctx->buffered = 0;
        }
    }

    /* then take whole blocks straight from the caller's buffer */
    while (len >= SHA256_BLOCK_SIZE) {
        sha256_block(ctx, p);
        p += SHA256_BLOCK_SIZE;
        len -= SHA256_BLOCK_SIZE;
    }

    /* and keep whatever is left for next time */
    if (len > 0) {
        memcpy(ctx->buffer, p, len);
        ctx->buffered = len;
    }
}

void sha256_final(sha256_ctx *ctx, unsigned char digest[SHA256_DIGEST_SIZE])
{
    uint64_t bits = ctx->bit_count;

    /* padding always starts with a single 1 bit, i.e. the byte 0x80 */
    unsigned char one = 0x80;
    sha256_update(ctx, &one, 1);
    ctx->bit_count = bits;   /* padding is not part of the message length */

    /* zeros until 8 bytes remain in the block, leaving room for the
     * length. If there is not room, this fills the block, it gets
     * processed, and the zeros continue into the next one. */
    unsigned char zero = 0;
    while (ctx->buffered != SHA256_BLOCK_SIZE - 8) {
        sha256_update(ctx, &zero, 1);
        ctx->bit_count = bits;
    }

    /* the original length in bits, big-endian, closes the last block */
    unsigned char length_bytes[8];
    for (int i = 0; i < 8; i++) {
        length_bytes[i] = (unsigned char)((bits >> (56 - 8 * i)) & 0xFF);
    }
    sha256_update(ctx, length_bytes, 8);

    /* the state, big-endian, is the digest */
    for (int i = 0; i < 8; i++) {
        digest[i * 4]     = (unsigned char)((ctx->state[i] >> 24) & 0xFF);
        digest[i * 4 + 1] = (unsigned char)((ctx->state[i] >> 16) & 0xFF);
        digest[i * 4 + 2] = (unsigned char)((ctx->state[i] >> 8) & 0xFF);
        digest[i * 4 + 3] = (unsigned char)(ctx->state[i] & 0xFF);
    }
}

int sha256_file(const char *path, unsigned char digest[SHA256_DIGEST_SIZE])
{
    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        return -1;
    }

    sha256_ctx ctx;
    sha256_init(&ctx);

    unsigned char buf[65536];
    size_t got;
    while ((got = fread(buf, 1, sizeof(buf), f)) > 0) {
        sha256_update(&ctx, buf, got);
    }
    int failed = ferror(f);
    fclose(f);
    if (failed) {
        return -1;
    }

    sha256_final(&ctx, digest);
    return 0;
}

void sha256_hex(const unsigned char *digest, size_t len, char *out)
{
    static const char hex[] = "0123456789abcdef";
    for (size_t i = 0; i < len; i++) {
        out[i * 2]     = hex[(digest[i] >> 4) & 0x0F];
        out[i * 2 + 1] = hex[digest[i] & 0x0F];
    }
    out[len * 2] = '\0';
}
