def other_fn():
    return 4


def dup_helper(x):
    """Same name as sub/mod.py:dup_helper — exercises the index's
    multi-definition behaviour for the same name across files."""
    return x - 1
