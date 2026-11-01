#include "inc/shared.h"
int api_close(int fd);
int api_twice(int x) { return x + x; }
static int helper(int);
struct ops { int (*open)(int); int (*twice)(int); int flags; };
static const struct ops b_ops = { .open = api_open, .twice = &api_twice, .flags = MAX_ITEMS };
static int (*table[])(int) = { api_close, helper };
int run(const struct ops *o, int v) {
    struct item it = { .v = v };
    item_t *p = &it;
    enum color c = GREEN;
    struct ops local = { .open = (int (*)(int))api_close };
    return o->open(v) + local.twice(p->v) + c + api_twice(v) + b_ops.flags;
}
