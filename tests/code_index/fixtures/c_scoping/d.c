struct ops2 { int (*cb)(int); };
extern int registered_only(int);
static const struct ops2 d_ops = { .cb = registered_only };
static int tool(int x) { return -x; }
int open_dev(int);
