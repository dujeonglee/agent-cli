"""``agent-cli web`` — LAN web UI for the agent loop.

Single-process, single-active-client. The HTTP layer (``server.py``)
drives one ``AgentLoop`` running on a worker thread and fans render
events out over Server-Sent Events to whichever client currently holds
the active connection. New connections take over from older ones.

Optional dependency: install with ``pip install agent-cli[web]``.
"""

from agent_cli.web.server import WebServer, create_app

__all__ = ["WebServer", "create_app"]
