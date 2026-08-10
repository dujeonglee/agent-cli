"""TaskBook configuration.

Reads simple ``key = value`` lines from ``taskbook.conf``.

Documented defaults (pair-3 P1: these are documented but NOT implemented —
a missing key currently crashes with a raw KeyError):

    db_file      tasks.json
    date_format  %Y-%m-%d
    max_open     100
"""


CONF_FILE = "taskbook.conf"


def load_config():
    settings = {}
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                settings[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return settings


def get(settings, key):
    # Pair-3 P1: implement the documented defaults instead of crashing.
    return settings[key]
