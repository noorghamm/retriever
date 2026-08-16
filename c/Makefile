# Build the retriever C client.
#
#   make           build ./retriever
#   make debug     rebuild with address and undefined-behaviour sanitizers
#   make clean     remove build output
#
# -Wall -Wextra turn on the warnings worth having; -Werror makes them
# errors, so a warning cannot be ignored into a habit.

# -D_POSIX_C_SOURCE: getaddrinfo, recv, send and close are POSIX, not
# ISO C. Asking for strict ISO with -std=c11 makes glibc hide every
# POSIX declaration unless a level is requested. macOS headers are
# laxer, so without this the code builds on a Mac and fails on Linux.
# It lives here rather than in a source file so it applies to every
# translation unit no matter what order the includes are in.

CC      ?= cc
CFLAGS  ?= -std=c11 -D_POSIX_C_SOURCE=200112L -Wall -Wextra -Werror -O2
OBJECTS  = frame.o sha256.o transfer.o main.o
TARGET   = retriever
HASHTOOL = sha256tool

.PHONY: all debug clean

all: $(TARGET) $(HASHTOOL)

$(TARGET): $(OBJECTS)
	$(CC) $(CFLAGS) -o $@ $(OBJECTS)

# hashes stdin and prints hex; the test suite checks it against hashlib
$(HASHTOOL): sha256.o sha256_main.o
	$(CC) $(CFLAGS) -o $@ sha256.o sha256_main.o

# each .o depends on the headers too, so editing one triggers a rebuild
%.o: %.c retriever.h sha256.h
	$(CC) $(CFLAGS) -c $<

# Sanitizers catch memory errors at runtime: out-of-bounds access, use
# after free, undefined behaviour. valgrind does the same job on Linux
# but does not support Apple Silicon, so this is the local equivalent.
debug: CFLAGS += -g -O0 -fsanitize=address,undefined
debug: clean all

clean:
	rm -f $(OBJECTS) sha256_main.o $(TARGET) $(HASHTOOL)
