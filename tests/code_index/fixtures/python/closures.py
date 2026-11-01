"""Closures and nested functions — v10.95.0 (DESIGN §20) innermost-function fixture."""

import functools


def leaf():
    return 1


def twig():
    return 2


def make_adder(n):
    def add(x):
        return leaf() + x

    twig()
    return add


def nest3():
    def mid():
        def core():
            return leaf()

        twig()
        return core

    leaf()
    return mid


def decorated_outer(fn):
    @functools.wraps(fn)
    def wrapped(*a):
        return fn(*a)

    twig()
    return wrapped


class Holder:
    value = leaf()

    def method(self):
        def local():
            return twig()

        return leaf()
