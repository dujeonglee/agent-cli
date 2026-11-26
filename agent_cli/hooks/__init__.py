"""Hook system — Python + shell lifecycle hooks for agent-cli.

Re-exports the shell hook API.
"""

from agent_cli.hooks.shell import (
    load_hooks,
    merge_hooks_configs,
    parse_hooks_config,
    run_hooks,
)

__all__ = [
    "load_hooks",
    "merge_hooks_configs",
    "parse_hooks_config",
    "run_hooks",
]
