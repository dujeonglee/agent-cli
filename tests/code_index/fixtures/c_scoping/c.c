#include "inc/shared.h"
int other(void) { return api_twice(1); }
int item_count(struct item *item) { return item->v + RED; }
int registered_only(int x) { return x; }
#define X 1
int ux(void) { return X; }
#undef X
#define X 2
int ux2(void) { return X; }
int tool(int x);
int use_tool(void) { return tool(1); }
struct ops3 { int (*cb)(int); };
int reg_local(void) { struct ops3 o = { .cb = api_twice }; return o.cb(1); }
struct dev { int (*open_dev)(int); };
int probe(struct dev *p) { return p->open_dev(1); }
