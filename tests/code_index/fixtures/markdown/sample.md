# A

Intro under top-level heading A.

## B

Sibling text under B.

### C

Deepest nesting: C's parent is B, and B's parent is A.

```python
# This fenced code block is NOT a heading and must NOT be parsed as a section.
def fake():
    return 0
```

### D

D is a sibling of C under B (both level-3, parent=B).

## E

E is a sibling of B (both level-2, parent=A). C and D are uncles of E.

# F

F is a new top-level (level-1) — no parent. Closes A's section.

## G

G's parent is F.

#### H4

Level-4 under G — parent=G (G is the nearest strictly-shallower).

##### H5

Level-5 — parent=H4.

###### H6

Level-6 — parent=H5; this is the deepest ATX level allowed.

Setext Title One
================

Setext h1 — kind_raw=setext_heading_1. Parent=None (it's a level-1 like F).

Setext Title Two
----------------

Setext h2 — kind_raw=setext_heading_2. Parent=`Setext Title One`.
## TightSibling
No blank line above this heading — tests the end_line edge where the previous section ends on the line immediately before.
