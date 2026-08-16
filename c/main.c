/*
 * main.c - the retriever C client.
 *
 * Speaks protocol v3 to the same server as the Python client. Today it
 * implements HELLO, LIST and QUIT; GET and PUT come next.
 *
 * Usage: retriever <host> <port> list
 */
#include "retriever.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* exit codes, matching the Python client so scripts see one contract */
#define EXIT_SERVER 1
#define EXIT_LOCAL  2
#define EXIT_CONN   3

/*
 * Complete the handshake: send HELLO, expect OK.
 * Returns 0 if the server accepted our version.
 */
static int do_hello(int fd)
{
    if (rtrv_write_frame(fd, RTRV_T_HELLO, NULL, 0) < 0) {
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
    free(payload);  /* free(NULL) is defined and does nothing, so this is safe */

    if (type != RTRV_T_OK) {
        fprintf(stderr, "error: unexpected reply 0x%02X to HELLO\n", type);
        return -1;
    }
    return 0;
}

/*
 * Ask for the directory listing and print one name per line.
 *
 * The payload is filenames joined by a zero byte, which is why we walk
 * it with an index instead of using string functions: a C string ends
 * at the first zero byte, and here those bytes are the separators.
 */
static int do_list(int fd)
{
    if (rtrv_write_frame(fd, RTRV_T_LIST, NULL, 0) < 0) {
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
    if (type != RTRV_T_OK) {
        fprintf(stderr, "error: unexpected reply 0x%02X to LIST\n", type);
        free(payload);
        return -1;
    }

    uint64_t start = 0;
    for (uint64_t i = 0; i <= payload_len; i++) {
        /* a name ends at a zero byte or at the end of the payload */
        if (i == payload_len || payload[i] == 0) {
            if (i > start) {
                printf("%.*s\n", (int)(i - start), (const char *)payload + start);
            }
            start = i + 1;
        }
    }

    free(payload);
    return 0;
}

/* Say goodbye politely and read the acknowledgement. */
static void do_quit(int fd)
{
    uint8_t type = 0;
    unsigned char *payload = NULL;
    uint64_t payload_len = 0;

    if (rtrv_write_frame(fd, RTRV_T_QUIT, NULL, 0) == 0) {
        if (rtrv_read_frame(fd, &type, &payload, &payload_len) == 0) {
            free(payload);
        }
    }
}

static void usage(const char *program)
{
    fprintf(stderr,
            "Usage:\n"
            "  %s <host> <port> list\n"
            "  %s <host> <port> get <filename> [-o local_name]\n"
            "  %s <host> <port> put <local_file> [remote_name]\n",
            program, program, program);
}

/* the last path component, used when no local name is given */
static const char *basename_of(const char *path)
{
    const char *slash = strrchr(path, '/');
    return slash ? slash + 1 : path;
}

int main(int argc, char **argv)
{
    if (argc < 4) {
        usage(argv[0]);
        return EXIT_LOCAL;
    }
    const char *host = argv[1];
    const char *port = argv[2];
    const char *command = argv[3];

    /* work out what each command needs before opening a connection, so
     * a usage mistake never costs the server a session */
    const char *name = NULL;      /* remote name for get, local path for put */
    const char *other = NULL;     /* -o target for get, remote name for put */

    if (strcmp(command, "list") == 0) {
        if (argc != 4) {
            usage(argv[0]);
            return EXIT_LOCAL;
        }
    } else if (strcmp(command, "get") == 0) {
        if (argc != 5 && argc != 7) {
            usage(argv[0]);
            return EXIT_LOCAL;
        }
        name = argv[4];
        if (argc == 7) {
            if (strcmp(argv[5], "-o") != 0) {
                usage(argv[0]);
                return EXIT_LOCAL;
            }
            other = argv[6];
        } else {
            other = basename_of(name);
        }
    } else if (strcmp(command, "put") == 0) {
        if (argc != 5 && argc != 6) {
            usage(argv[0]);
            return EXIT_LOCAL;
        }
        name = argv[4];
        other = (argc == 6) ? argv[5] : basename_of(name);
    } else {
        fprintf(stderr, "error: unknown command '%s'\n", command);
        usage(argv[0]);
        return EXIT_LOCAL;
    }

    int fd = rtrv_connect(host, port);
    if (fd < 0) {
        return EXIT_CONN;
    }

    int status = EXIT_SUCCESS;
    if (do_hello(fd) < 0) {
        status = EXIT_CONN;
    } else {
        int rc;
        if (strcmp(command, "list") == 0) {
            rc = do_list(fd);
        } else if (strcmp(command, "get") == 0) {
            rc = rtrv_do_get(fd, name, other);
        } else {
            rc = rtrv_do_put(fd, name, other);
        }
        if (rc < 0) {
            status = EXIT_SERVER;
        } else {
            do_quit(fd);
        }
    }

    close(fd);
    return status;
}
