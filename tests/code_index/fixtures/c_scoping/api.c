#include "inc/shared.h"
int global_count = 0;
int api_open(int fd) { return fd + global_count; }
static int helper(int x) { return x + 1; }
int api_close(int fd) { return helper(fd); }
int tool(int x) { return x; }
