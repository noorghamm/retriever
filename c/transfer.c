/*
 * transfer.c - GET and PUT in C, with resume and hash verification.
 *
 * This is where C stops being about bytes on a socket and starts being
 * about files on a disk: partial downloads, offsets, and the rule that
 * nothing is presented as finished until its hash checks out.
 */
#include "retriever.h"
#include "sha256.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* --- paths and partial files ------------------------------------------- */

/*
 * Split "dir/file" into its directory and its last component.
 *
 * The standard dirname()/basename() are allowed to modify their argument
 * and to return pointers to static storage, which makes them awkward and
 * surprising. Doing it by hand is clearer and safer here.
 */
void rtrv_split_path(const char *path, char *dir, size_t dir_size,
                     char *base, size_t base_size)
{
    const char *slash = strrchr(path, '/');
    if (slash == NULL) {
        snprintf(dir, dir_size, ".");
        snprintf(base, base_size, "%s", path);
    } else {
        size_t dir_len = (size_t)(slash - path);
        if (dir_len == 0) {
            dir_len = 1;   /* the path "/file" lives in "/" */
        }
        if (dir_len >= dir_size) {
            dir_len = dir_size - 1;
        }
        memcpy(dir, path, dir_len);
        dir[dir_len] = '\0';
        snprintf(base, base_size, "%s", slash + 1);
    }
}

/*
 * Build the partial file name for a transfer:  .<name>.<token hex>.part
 * The resume token in the name is what stops a resume from splicing one
 * file's bytes onto another's.
 */
void rtrv_partial_name(const char *dest, const unsigned char *digest,
                       char *out, size_t out_size)
{
    char dir[RTRV_PATH_MAX];
    char base[RTRV_PATH_MAX];
    char token[RTRV_TOKEN_SIZE * 2 + 1];

    rtrv_split_path(dest, dir, sizeof(dir), base, sizeof(base));
    sha256_hex(digest, RTRV_TOKEN_SIZE, token);

    if (strcmp(dir, ".") == 0 && strchr(dest, '/') == NULL) {
        snprintf(out, out_size, ".%s.%s.part", base, token);
    } else {
        snprintf(out, out_size, "%s/.%s.%s.part", dir, base, token);
    }
}

/*
 * Look for an existing partial for this destination and, if exactly one
 * is found, report its path, size and resume token.
 *
 * Returns 1 when a partial was found, 0 when none was, -1 on error.
 */
int rtrv_find_partial(const char *dest, char *path_out, size_t path_size,
                      uint64_t *size_out, unsigned char *token_out)
{
    char dir[RTRV_PATH_MAX];
    char base[RTRV_PATH_MAX];
    rtrv_split_path(dest, dir, sizeof(dir), base, sizeof(base));

    DIR *d = opendir(dir);
    if (d == NULL) {
        return -1;
    }

    char prefix[RTRV_PATH_MAX];
    snprintf(prefix, sizeof(prefix), ".%s.", base);
    size_t prefix_len = strlen(prefix);

    int found = 0;
    char match[RTRV_PATH_MAX];
    struct dirent *entry;

    while ((entry = readdir(d)) != NULL) {
        size_t name_len = strlen(entry->d_name);
        /* wanted: <prefix><16 hex chars>.part */
        if (name_len != prefix_len + RTRV_TOKEN_SIZE * 2 + 5) {
            continue;
        }
        if (strncmp(entry->d_name, prefix, prefix_len) != 0) {
            continue;
        }
        if (strcmp(entry->d_name + name_len - 5, ".part") != 0) {
            continue;
        }
        found++;
        snprintf(match, sizeof(match), "%s", entry->d_name);
    }
    closedir(d);

    if (found != 1) {
        return 0;   /* none, or too many to choose between */
    }

    if (strcmp(dir, ".") == 0 && strchr(dest, '/') == NULL) {
        snprintf(path_out, path_size, "%s", match);
    } else {
        snprintf(path_out, path_size, "%s/%s", dir, match);
    }

    struct stat st;
    if (stat(path_out, &st) != 0) {
        return 0;
    }
    *size_out = (uint64_t)st.st_size;

    /* the token is the 16 hex characters before ".part" */
    const char *hex = match + strlen(match) - 5 - RTRV_TOKEN_SIZE * 2;
    for (size_t i = 0; i < RTRV_TOKEN_SIZE; i++) {
        unsigned value;
        if (sscanf(hex + i * 2, "%2x", &value) != 1) {
            return 0;
        }
        token_out[i] = (unsigned char)value;
    }
    return 1;
}

static int file_exists(const char *path)
{
    struct stat st;
    return stat(path, &st) == 0;
}

/* --- GET ---------------------------------------------------------------- */

int rtrv_do_get(int fd, const char *remote_name, const char *dest)
{
    if (file_exists(dest)) {
        fprintf(stderr, "error: '%s' already exists locally, use -o to pick another name\n",
                dest);
        return -1;
    }

    /* offer whatever partial we already hold, so the server can send
     * only the bytes we are missing */
    char existing[RTRV_PATH_MAX];
    uint64_t offset = 0;
    unsigned char token[RTRV_TOKEN_SIZE];
    memset(token, 0, sizeof(token));
    int has_partial = rtrv_find_partial(dest, existing, sizeof(existing),
                                        &offset, token);
    if (has_partial != 1) {
        offset = 0;
        memset(token, 0, sizeof(token));
    }

    size_t name_len = strlen(remote_name);
    if (name_len > RTRV_MAX_NAME) {
        fprintf(stderr, "error: filename too long\n");
        return -1;
    }

    /* payload: u64 offset, 8-byte token, u16 name_len, name */
    unsigned char request[8 + RTRV_TOKEN_SIZE + 2 + RTRV_MAX_NAME];
    rtrv_put_u64(request, offset);
    memcpy(request + 8, token, RTRV_TOKEN_SIZE);
    rtrv_put_u16(request + 8 + RTRV_TOKEN_SIZE, (uint16_t)name_len);
    memcpy(request + 8 + RTRV_TOKEN_SIZE + 2, remote_name, name_len);

    if (rtrv_write_frame(fd, RTRV_T_GET, request,
                         8 + RTRV_TOKEN_SIZE + 2 + name_len) < 0) {
        return -1;
    }

    uint8_t type = 0;
    uint64_t payload_len = 0;
    if (rtrv_read_frame_header(fd, &type, &payload_len) < 0) {
        return -1;
    }
    if (type == RTRV_T_ERROR) {
        if (payload_len > RTRV_PAYLOAD_CAP) {
            fprintf(stderr, "error: oversized error frame\n");
            return -1;
        }
        unsigned char *payload = malloc((size_t)payload_len);
        if (payload == NULL) {
            fprintf(stderr, "error: out of memory\n");
            return -1;
        }
        if (rtrv_read_exact(fd, payload, (size_t)payload_len) == 0) {
            rtrv_report_error(payload, payload_len);
        }
        free(payload);
        return -1;
    }
    if (type != RTRV_T_OK) {
        fprintf(stderr, "error: unexpected reply 0x%02X to GET\n", type);
        return -1;
    }

    /* metadata block: u64 total_size, u64 start_offset, 32-byte hash */
    unsigned char meta[RTRV_GET_META];
    if (rtrv_read_exact(fd, meta, sizeof(meta)) < 0) {
        return -1;
    }
    uint64_t total_size = rtrv_get_u64(meta);
    uint64_t start = rtrv_get_u64(meta + 8);
    const unsigned char *digest = meta + 16;

    char partial[RTRV_PATH_MAX];
    rtrv_partial_name(dest, digest, partial, sizeof(partial));

    if (start > 0) {
        printf("resuming '%s' from byte %llu\n",
               remote_name, (unsigned long long)start);
    } else if (has_partial == 1) {
        /* the server offered a different file: our old bytes are useless */
        remove(existing);
    }

    FILE *out = fopen(partial, start > 0 ? "ab" : "wb");
    if (out == NULL) {
        fprintf(stderr, "error: cannot write '%s': %s\n", partial, strerror(errno));
        return -1;
    }

    unsigned char buf[65536];
    uint64_t remaining = total_size - start;
    while (remaining > 0) {
        size_t want = remaining < sizeof(buf) ? (size_t)remaining : sizeof(buf);
        if (rtrv_read_exact(fd, buf, want) < 0) {
            /* the partial survives on purpose: it is what a retry resumes from */
            fclose(out);
            fprintf(stderr, "error: download interrupted (partial kept for resume)\n");
            return -1;
        }
        if (fwrite(buf, 1, want, out) != want) {
            fclose(out);
            fprintf(stderr, "error: cannot write to '%s'\n", partial);
            return -1;
        }
        remaining -= want;
    }
    if (fclose(out) != 0) {
        fprintf(stderr, "error: cannot flush '%s'\n", partial);
        return -1;
    }

    unsigned char actual[SHA256_DIGEST_SIZE];
    if (sha256_file(partial, actual) < 0) {
        fprintf(stderr, "error: cannot re-read '%s' to verify it\n", partial);
        return -1;
    }
    if (memcmp(actual, digest, SHA256_DIGEST_SIZE) != 0) {
        remove(partial);
        fprintf(stderr, "error: downloaded content did not match the server's hash\n");
        return -1;
    }

    if (rename(partial, dest) != 0) {
        fprintf(stderr, "error: cannot rename '%s' to '%s': %s\n",
                partial, dest, strerror(errno));
        return -1;
    }

    printf("downloaded '%s' (%llu bytes, hash verified) -> %s\n",
           remote_name, (unsigned long long)total_size, dest);
    return 0;
}

/* --- PUT ---------------------------------------------------------------- */

int rtrv_do_put(int fd, const char *local_path, const char *remote_name)
{
    struct stat st;
    if (stat(local_path, &st) != 0 || !S_ISREG(st.st_mode)) {
        fprintf(stderr, "error: '%s' not found locally\n", local_path);
        return -1;
    }
    uint64_t file_size = (uint64_t)st.st_size;

    unsigned char digest[SHA256_DIGEST_SIZE];
    if (sha256_file(local_path, digest) < 0) {
        fprintf(stderr, "error: cannot read '%s'\n", local_path);
        return -1;
    }

    size_t name_len = strlen(remote_name);
    if (name_len > RTRV_MAX_NAME) {
        fprintf(stderr, "error: filename too long\n");
        return -1;
    }

    /* step 1: u64 file_size, 32-byte hash, u16 name_len, name */
    unsigned char request[8 + SHA256_DIGEST_SIZE + 2 + RTRV_MAX_NAME];
    rtrv_put_u64(request, file_size);
    memcpy(request + 8, digest, SHA256_DIGEST_SIZE);
    rtrv_put_u16(request + 8 + SHA256_DIGEST_SIZE, (uint16_t)name_len);
    memcpy(request + 8 + SHA256_DIGEST_SIZE + 2, remote_name, name_len);

    if (rtrv_write_frame(fd, RTRV_T_PUT, request,
                         8 + SHA256_DIGEST_SIZE + 2 + name_len) < 0) {
        return -1;
    }

    uint8_t type = 0;
    unsigned char *payload = NULL;
    uint64_t payload_len = 0;
    if (rtrv_read_frame(fd, &type, &payload, &payload_len) < 0) {
        return -1;
    }
    if (type == RTRV_T_ERROR) {
        rtrv_report_error(payload, payload_len);
        free(payload);
        return -1;
    }
    if (type != RTRV_T_OK || payload_len < 8) {
        fprintf(stderr, "error: unexpected reply 0x%02X to PUT\n", type);
        free(payload);
        return -1;
    }
    uint64_t resume_offset = rtrv_get_u64(payload);
    free(payload);

    if (resume_offset > file_size) {
        fprintf(stderr, "error: server asked to resume past the end of the file\n");
        return -1;
    }
    if (resume_offset > 0) {
        printf("resuming upload from byte %llu\n", (unsigned long long)resume_offset);
    }

    /* step 2: send only the bytes the server is missing */
    FILE *in = fopen(local_path, "rb");
    if (in == NULL) {
        fprintf(stderr, "error: cannot open '%s': %s\n", local_path, strerror(errno));
        return -1;
    }
    if (fseeko(in, (off_t)resume_offset, SEEK_SET) != 0) {
        fclose(in);
        fprintf(stderr, "error: cannot seek in '%s'\n", local_path);
        return -1;
    }

    unsigned char buf[65536];
    uint64_t remaining = file_size - resume_offset;
    while (remaining > 0) {
        size_t want = remaining < sizeof(buf) ? (size_t)remaining : sizeof(buf);
        size_t got = fread(buf, 1, want, in);
        if (got != want) {
            fclose(in);
            fprintf(stderr, "error: '%s' changed size while uploading\n", local_path);
            return -1;
        }
        if (rtrv_write_all(fd, buf, got) < 0) {
            fclose(in);
            return -1;
        }
        remaining -= got;
    }
    fclose(in);

    /* the verdict: the server has hashed what it stored */
    if (rtrv_read_frame(fd, &type, &payload, &payload_len) < 0) {
        return -1;
    }
    if (type == RTRV_T_ERROR) {
        rtrv_report_error(payload, payload_len);
        free(payload);
        return -1;
    }
    free(payload);
    if (type != RTRV_T_OK) {
        fprintf(stderr, "error: unexpected reply 0x%02X after upload\n", type);
        return -1;
    }

    printf("uploaded '%s' (%llu bytes, hash verified) -> %s\n",
           local_path, (unsigned long long)file_size, remote_name);
    return 0;
}
