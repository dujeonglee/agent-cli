"""``agent-cli web`` — browser room for one agent session.

One process, one session, many viewers. The HTTP layer (``server.py``)
drives the session's loop on a worker thread and fans render events out
over Server-Sent Events to every authenticated connection; every viewer
may also send input (no controller/observer split). Binds to 127.0.0.1 by
default — ``--host 0.0.0.0`` exposes it on the LAN (opt-in).

Optional dependency: install with ``pip install agent-cli[web]``.
"""

from agent_cli.web.server import WebServer, create_app

__all__ = ["WebServer", "create_app"]
