#include "inc/shared.h"
#define LOCAL_MAX 3
static int helper(int x) { return x * 2; }
int api_twice(int x) { return x * 2; }
static int uses(int arg) {
    int r = helper(arg);
    r += api_open(r);
    r += clamp(r);
    r += SQUARE(r) + MAX_ITEMS + LOCAL_MAX;
    r += nowhere();
    r += api_twice(r);
    return r + global_count;
}
static int shadow(int api_open) {
    int clamp = 1;
    {
        int helper = 2;
        api_open += helper;
    }
    for (int i = 0; i < 1; i++) { int nowhere = i; clamp += nowhere; }
    return api_open + clamp + helper(1) + uses(0);
}
