"""Fixture for Python walker tests."""
from typing import Optional

MAX_RETRIES = 3
default_timeout = 30


def helper(x: int) -> int:
    return x * 2


class Service:
    instance_count = 0

    def __init__(self, name: str):
        self.name = name

    @property
    def label(self) -> str:
        return f"<{self.name}>"

    @staticmethod
    def make(name):
        return Service(name)

    async def process(self, payload):
        return helper(len(payload))


class DerivedService(Service):
    def process(self, payload):
        return helper(MAX_RETRIES)
