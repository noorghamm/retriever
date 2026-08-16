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
OBJECTS  = frame.o main.o
TARGET   = retriever

.PHONY: all debug clean

all: $(TARGET)

$(TARGET): $(OBJECTS)
	$(CC) $(CFLAGS) -o $@ $(OBJECTS)

# each .o depends on the header too, so editing it triggers a rebuild
%.o: %.c retriever.h
	$(CC) $(CFLAGS) -c $<

# Sanitizers catch memory errors at runtime: out-of-bounds access, use
# after free, undefined behaviour. valgrind does the same job on Linux
# but does not support Apple Silicon, so this is the local equivalent.
debug: CFLAGS += -g -O0 -fsanitize=address,undefined
debug: clean $(TARGET)

clean:
	rm -f $(OBJECTS) $(TARGET)
