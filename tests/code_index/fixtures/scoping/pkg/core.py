import os
import os.path as osp


def start():
    return 1


def stop():
    return 0


class Engine:
    limit = start()

    def run(self):
        self.step()
        return self.limit

    def step(self):
        return stop()

    @classmethod
    def make(cls):
        return cls.build()

    @classmethod
    def build(cls):
        return os.getcwd() or osp.join("a", "b")


class Turbo(Engine):
    def step(self):
        return super().step()

    def boost(self):
        return self.run()
