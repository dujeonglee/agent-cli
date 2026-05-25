#include "sample.h"

#define MAX_BUF 1024
#define DBG(fmt, ...) printf(fmt, __VA_ARGS__)

static struct point origin = {0, 0};

static int helper(int x) {
    return x * 2;
}

int compute(struct point *p) {
    DBG("computing %d\n", p->x);
    return helper(p->x) + helper(p->y);
}
