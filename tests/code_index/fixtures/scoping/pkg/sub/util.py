from .. import core
from ..core import Engine as E
from ..core import stop


def shadowed(stop):
    return stop()


def local_alias():
    s = stop
    return s()


def late_local():
    first = later()
    later = None
    return first


def later():
    return 2


def calls_up():
    core.start()
    E().run()
    return stop()


def unknown(obj):
    return obj.step()


def comprehension(items):
    return [stop() for stop in items] + [later() for _ in items]


def walrus(items):
    if (n := len(items)) > 1:
        return n
    return later()
