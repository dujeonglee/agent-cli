"""Pluggable wire format system.

A wire format is the on-the-wire shape of a single LLM response —
prompt rules, parser, recovery messages, prefill, and provider quirks
bundled in one module. Plugins live in
``agent_cli/wire_formats/<name>.py`` and self-register at import time
via :func:`register`.

The loop / prompts / recovery layers depend only on
:class:`WireFormat` (Protocol) and :class:`ParsedAction` (data) — they
never branch on a plugin's name. New formats are added by dropping a
file into this directory; obsolete ones are deleted by removing the
file. Main code is not edited either way.

The CLI ``--response-format <name>`` option resolves through
:func:`get`. The default ``"react"`` plugin is registered when its
module is imported (see ``agent_cli/wire_formats/react.py``).
"""

from __future__ import annotations

from agent_cli.wire_formats.base import ParsedAction, WireFormat

# ── Registry ─────────────────────────────────────
_registry: dict[str, WireFormat] = {}


def register(wire_format: WireFormat) -> None:
    """Register a plugin under its ``name`` attribute.

    Idempotent on identity (re-registering the same instance is a no-op);
    raises ``ValueError`` on a name collision with a *different* instance
    so accidental shadowing is loud rather than silent.

    Plugins call this at the bottom of their module:

        register(ReActFormat())
    """
    name = wire_format.name
    existing = _registry.get(name)
    if existing is wire_format:
        return
    if existing is not None:
        raise ValueError(
            f"Wire format '{name}' is already registered to a different "
            f"instance. Each plugin module should register exactly once."
        )
    _registry[name] = wire_format


def get(name: str) -> WireFormat:
    """Return the registered plugin for ``name``.

    Raises ``KeyError`` with the list of available names if no plugin is
    registered under ``name`` — the list is what the CLI's ``--response-format``
    option would accept.
    """
    plugin = _registry.get(name)
    if plugin is None:
        available = ", ".join(sorted(_registry)) or "(none)"
        raise KeyError(
            f"Wire format '{name}' is not registered. Available: {available}."
        )
    return plugin


def list_names() -> list[str]:
    """Return the sorted list of registered plugin names.

    Used by the CLI to populate help text / validate ``--response-format``
    values.
    """
    return sorted(_registry)


__all__ = ["ParsedAction", "WireFormat", "register", "get", "list_names"]


# ── Builtin plugin registration ──────────────────────────────
# Plugins shipped with agent-cli register at package-import time so
# ``get("react")`` works out of the box. The import is at the bottom
# (not the top) so the ``register`` symbol it depends on is already
# defined when ``react.py`` is loaded — the alternative (top-level
# import + explicit register call) would fail because ``react`` would
# not yet see ``register`` in this module's namespace.
def _register_builtin_plugins() -> None:
    from agent_cli.wire_formats.react import ReActFormat

    register(ReActFormat())


_register_builtin_plugins()
