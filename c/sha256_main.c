/*
 * sha256_main.c - test harness: hash stdin, print the hex digest.
 *
 * Exists so the test suite can compare this implementation against
 * Python's hashlib on the same bytes. A hand-written hash is only
 * trustworthy if something independent checks it.
 */
#include "sha256.h"

#include <stdio.h>

int main(void)
{
    sha256_ctx ctx;
    sha256_init(&ctx);

    unsigned char buf[65536];
    size_t got;
    while ((got = fread(buf, 1, sizeof(buf), stdin)) > 0) {
        sha256_update(&ctx, buf, got);
    }
    if (ferror(stdin)) {
        fprintf(stderr, "error: could not read stdin\n");
        return 1;
    }

    unsigned char digest[SHA256_DIGEST_SIZE];
    sha256_final(&ctx, digest);

    char hex[SHA256_DIGEST_SIZE * 2 + 1];
    sha256_hex(digest, SHA256_DIGEST_SIZE, hex);
    printf("%s\n", hex);
    return 0;
}
