"""Extra Python snippets beyond app.py for DESIGN §12.1 coverage.

Each snippet documents what walker case it exercises.
"""


def with_defaults(a, b=2, *args, **kwargs):
    """Default + variadic + kwargs — modifier vocab is open in Python
    walker, but the function should still be recorded as kind=function.
    """
    return a + b


def outer():
    """Function nested inside function — inner.parent should be 'outer'."""

    def inner():
        return 1

    return inner


class Outer:
    """Inner class case — Inner.parent should be 'Outer'."""

    class Inner:
        def deep(self):
            return 1
