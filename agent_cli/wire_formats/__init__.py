"""Compatibility shim — ``agent_cli.wire_formats`` moved to :mod:`agent_cli.dialects` (v10.0.0).

Everything the old package exported is re-exported here under both the new
and the old names, and importing this module raises a ``DeprecationWarning``.
The shim is removed in v11 — switch to ``from agent_cli.dialects import …``.
"""

from __future__ import annotations

import warnings

from agent_cli.dialects import (
    DEFAULT_DIALECT,
    DialectBase,
    Op,
    ParsedAction,
    ParsedTurn,
    all_system_user_prefixes,
    dialect_for_model,
    get,
    list_names,
    register,
    resolve_dialect,
    try_foreign_parse,
)

warnings.warn(
    "agent_cli.wire_formats is deprecated since v10.0.0 — import agent_cli.dialects "
    "(removed in v11)",
    DeprecationWarning,
    stacklevel=2,
)

# 옛 이름 별칭
WireFormat = DialectBase
DEFAULT_WIRE_FORMAT = DEFAULT_DIALECT
wire_format_for_model = dialect_for_model
resolve_wire_format = resolve_dialect

__all__ = [
    "DEFAULT_DIALECT",
    "DEFAULT_WIRE_FORMAT",
    "DialectBase",
    "Op",
    "ParsedAction",
    "ParsedTurn",
    "WireFormat",
    "all_system_user_prefixes",
    "dialect_for_model",
    "get",
    "list_names",
    "register",
    "resolve_dialect",
    "resolve_wire_format",
    "try_foreign_parse",
    "wire_format_for_model",
]
