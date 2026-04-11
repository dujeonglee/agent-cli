"""Hook system — Python + shell lifecycle hooks for agent-cli.

Re-exports the shell hook API for backward compatibility.
"""

from agent_cli.hooks.shell import (  # noqa: F401
    HookEntry,
    HookMatcher,
    HookResult,
    load_hooks,
    parse_hooks_config,
    run_hooks,
)

__all__ = [
    "HookEntry",
    "HookMatcher",
    "HookResult",
    "load_hooks",
    "parse_hooks_config",
    "run_hooks",
]
