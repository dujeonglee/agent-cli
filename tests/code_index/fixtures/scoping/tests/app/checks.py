from layout.mod import from_src
from pkg import Engine, start


def engine_check():
    assert start()
    Engine().run()
    return from_src()
