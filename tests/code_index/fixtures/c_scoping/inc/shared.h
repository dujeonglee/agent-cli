#ifndef SHARED_H
#define SHARED_H
#define MAX_ITEMS 8
#define SQUARE(x) ((x) * (x))
struct item { int v; };
typedef struct item item_t;
enum color { RED, GREEN };
static inline int clamp(int v) { return v < 0 ? 0 : v; }
int api_open(int fd);
int api_twice(int x);
extern int global_count;
int nowhere(void);
#endif
