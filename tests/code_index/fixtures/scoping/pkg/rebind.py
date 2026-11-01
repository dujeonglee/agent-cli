from pkg.core import start

start()


def start():
    return 3


start()


def scoped():
    global start
    return start()


def outer():
    def start():
        return 4

    def inner():
        nonlocal_value = start()
        return nonlocal_value

    return inner()


def setter():
    global start
    start = outer
    return start()


class Holder:
    outer = None

    def method(self):
        return outer()


def counter():
    def stop():
        return 0

    def bump():
        nonlocal stop
        stop = None
        return stop()

    return bump()
