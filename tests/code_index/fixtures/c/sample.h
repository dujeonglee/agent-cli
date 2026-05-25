#ifndef SAMPLE_H
#define SAMPLE_H

struct point {
    int x;
    int y;
};

typedef unsigned int u32;

enum color { RED, GREEN, BLUE };

int compute(struct point *p);

#endif
