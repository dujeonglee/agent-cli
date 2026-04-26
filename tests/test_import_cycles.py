"""Cold-start import regression tests.

These tests guard against module-level import cycles that only manifest
when a specific module is the *first* one loaded — i.e. when the CLI is
invoked directly. The pytest collector usually loads many submodules
first, populating ``sys.modules`` so cycles silently resolve. A fresh
subprocess is the only way to reproduce the cold-start order.

Each test runs ``python -c 'import agent_cli.X'`` in a subprocess and
asserts a clean exit. If a cycle is reintroduced (e.g. a recovery-layer
module starts importing from ``tools`` or ``context`` at module level
again), this catches it before the user does.

History: a Step 4a edit added ``from agent_cli.tools.registry import
validate_tool_input`` at the top of ``recovery/detectors.py``, creating
a cycle through ``constants → recovery → detectors → tools → context →
constants``. CLI startup failed; the test suite stayed green because no
test entered through ``agent_cli.main``.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


COLD_START_ENTRY_POINTS = [
    # CLI startup path. Was the actual broken case.
    "agent_cli.main",
    # constants is imported very early by main and config; if it's the
    # first thing loaded, it must finish without re-entry.
    "agent_cli.constants",
    # Recovery package surface — used by both loop and constants.
    "agent_cli.recovery",
    # Tools package surface — transitively imports context.
    "agent_cli.tools",
]


@pytest.mark.parametrize("module", COLD_START_ENTRY_POINTS)
def test_cold_start_import(module):
    """Importing ``module`` in a fresh interpreter must not raise.

    Subprocess isolation guarantees a clean ``sys.modules``; pytest's
    own collection order would otherwise mask import cycles.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Cold-start `import {module}` failed.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
