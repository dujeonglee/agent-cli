"""Tests for the pluggable renderer system."""

from unittest.mock import MagicMock

from rich.console import Console

import agent_cli.render.minimal as minimal_mod
from agent_cli.render import (
    load_renderer_by_name,
    set_renderer,
    get_renderer,
)
from agent_cli.render.base import ConfirmOption
from agent_cli.render.minimal import MinimalRenderer


class _FakeStream:
    """Minimal stdin/stdout stand-in with a controllable ``isatty``."""

    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestMinimalCanPrompt:
    """``can_prompt`` gates the dangerous-shell prompt — it must reflect
    whether a real terminal is attached (not Live/thread state, which
    ``confirm`` itself handles)."""

    def test_true_when_both_tty(self, monkeypatch):
        r = MinimalRenderer(Console())
        monkeypatch.setattr(minimal_mod.sys, "stdin", _FakeStream(True))
        monkeypatch.setattr(minimal_mod.sys, "stdout", _FakeStream(True))
        assert r.can_prompt() is True

    def test_false_when_stdin_not_tty(self, monkeypatch):
        r = MinimalRenderer(Console())
        monkeypatch.setattr(minimal_mod.sys, "stdin", _FakeStream(False))
        monkeypatch.setattr(minimal_mod.sys, "stdout", _FakeStream(True))
        assert r.can_prompt() is False

    def test_true_even_with_active_live(self, monkeypatch):
        """An active Live region does NOT make ``can_prompt`` False —
        ``confirm`` pauses the Live, so a TTY is the only precondition."""
        r = MinimalRenderer(Console())
        r._parallel_live = MagicMock()
        monkeypatch.setattr(minimal_mod.sys, "stdin", _FakeStream(True))
        monkeypatch.setattr(minimal_mod.sys, "stdout", _FakeStream(True))
        assert r.can_prompt() is True


class TestMinimalConfirmPausesLive:
    """Inside a parallel-delegate Live panel the prompt would be painted
    over; ``confirm`` must stop the Live for the read and restart it."""

    def test_active_parallel_live_paused_and_resumed(self, monkeypatch):
        r = MinimalRenderer(Console())
        live = MagicMock()
        r._parallel_live = live
        monkeypatch.setattr("builtins.input", lambda prompt="": "y do it")

        key, comment = r.confirm(
            "ok? ", [ConfirmOption(key="y", label="yes")], default_key="n"
        )

        assert key == "y"
        assert comment == "do it"
        live.stop.assert_called_once()
        live.start.assert_called_once()

    def test_live_resumed_even_on_eof(self, monkeypatch):
        """EOF returns the default deny, but the Live must still resume."""

        def _raise(prompt=""):
            raise EOFError

        r = MinimalRenderer(Console())
        live = MagicMock()
        r._live = live
        monkeypatch.setattr("builtins.input", _raise)

        key, _ = r.confirm(
            "ok? ", [ConfirmOption(key="y", label="yes")], default_key="n"
        )

        assert key == "n"
        live.stop.assert_called_once()
        live.start.assert_called_once()

    def test_no_live_reads_directly(self, monkeypatch):
        """No active Live (main loop / single delegate) → plain read."""
        r = MinimalRenderer(Console())
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        key, _ = r.confirm(
            "ok? ",
            [ConfirmOption(key="y", label="yes"), ConfirmOption(key="n", label="no")],
            default_key="y",
        )
        assert key == "n"

    def test_prompt_user_pauses_active_live(self, monkeypatch):
        """``ask`` shares the same guard — prompt_user must also pause the
        Live panel so a delegate-worker question isn't painted over."""
        r = MinimalRenderer(Console())
        live = MagicMock()
        r._parallel_live = live
        monkeypatch.setattr("builtins.input", lambda prompt="": "the answer")

        value = r.prompt_user("Q: ", multiline=False)

        assert value == "the answer"
        live.stop.assert_called_once()
        live.start.assert_called_once()

    def test_prompt_user_resumes_live_on_eof(self, monkeypatch):
        """prompt_user propagates EOF (caller policy), but the Live must
        still resume via the guard's finally."""

        def _raise(prompt=""):
            raise EOFError

        r = MinimalRenderer(Console())
        live = MagicMock()
        r._parallel_live = live
        monkeypatch.setattr("builtins.input", _raise)

        try:
            r.prompt_user("Q: ", multiline=False)
        except EOFError:
            pass
        live.stop.assert_called_once()
        live.start.assert_called_once()


class TestInteractivePromptSerialization:
    """confirm and ask share one re-entrant lock so they serialize against
    each other across delegate worker threads."""

    def test_shared_lock_is_reentrant(self):
        # The dangerous-shell guard holds the lock then calls confirm,
        # which re-acquires it on the same thread — must not deadlock.
        from agent_cli.render import interactive_lock

        with interactive_lock:
            with interactive_lock:
                assert True


class TestPromptProvenance:
    """confirm/ask surface who (delegate agent) + why (reasoning) + what
    (action) so the user can attribute an out-of-context prompt."""

    def test_meta_empty_without_agent(self):
        # Main agent (no delegate label) → no header; its thought/action
        # already print inline above the prompt.
        r = MinimalRenderer(Console())
        r.note_thought("some reasoning")
        r.note_action("shell", "rm x")
        assert r._format_prompt_meta(include_action=True) == ""

    def test_meta_with_agent_first_line_only(self):
        r = MinimalRenderer(Console())
        r.set_thread_agent("explorer")
        r.note_thought("first line\nsecond line")
        r.note_action("shell", "rm -rf build")
        header = r._format_prompt_meta(include_action=True)
        assert "explorer" in header
        assert "first line" in header and "second line" not in header
        assert "rm -rf build" in header
        # ask form omits the action.
        assert "rm -rf build" not in r._format_prompt_meta(include_action=False)

    def test_confirm_prints_header_for_delegate(self, monkeypatch):
        from io import StringIO

        buf = StringIO()
        r = MinimalRenderer(Console(file=buf, force_terminal=False, width=100))
        r.set_thread_agent("explorer")
        r.note_thought("must delete the stale build dir")
        r.note_action("shell", "rm -rf build")
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        r.confirm("Allow? ", [ConfirmOption(key="y", label="yes")], default_key="n")
        out = buf.getvalue()
        assert "explorer" in out
        assert "must delete the stale build dir" in out

    def test_confirm_no_header_for_main_agent(self, monkeypatch):
        from io import StringIO

        buf = StringIO()
        r = MinimalRenderer(Console(file=buf, force_terminal=False, width=100))
        r.note_thought("main agent reasoning")  # no agent label set
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        r.confirm("Allow? ", [ConfirmOption(key="y", label="yes")], default_key="n")
        assert "main agent reasoning" not in buf.getvalue()

    def test_delegate_begin_sets_agent_end_clears(self):
        r = MinimalRenderer(Console())
        r.begin_delegate_task(task_id="t1", index=0, agent="explorer", task_text="x")
        assert r.prompt_meta()["agent"] == "explorer"
        r.end_delegate_task(task_id="t1", success=True, duration_s=0.1)
        assert r.prompt_meta()["agent"] == ""

    def test_delegate_unnamed_falls_back_to_task_index(self):
        r = MinimalRenderer(Console())
        r.begin_delegate_task(task_id="t1", index=2, agent="", task_text="x")
        assert r.prompt_meta()["agent"] == "task #3"
        r.end_delegate_task(task_id="t1", success=True, duration_s=0.1)


class TestLoadRendererByName:
    def test_load_minimal(self):
        old = get_renderer()
        load_renderer_by_name("minimal")
        assert isinstance(get_renderer(), MinimalRenderer)
        set_renderer(old)

    def test_load_nonexistent_raises(self):
        import pytest

        with pytest.raises(ValueError, match="not found"):
            load_renderer_by_name("nonexistent_renderer_xyz")

    def test_load_module_without_renderer_raises(self):
        """Module exists but has no Renderer subclass."""
        import pytest

        with pytest.raises(ValueError, match="No Renderer subclass"):
            load_renderer_by_name("base")  # base.py has ABC, not a concrete class


class TestBuildAgentDescriptions:
    def test_includes_builtin_explorer(self):
        from agent_cli.prompts.system_prompt import build_agent_descriptions

        desc = build_agent_descriptions()
        assert "explorer" in desc
        assert "Available Agents" in desc

    def test_excludes_disable_model_invocation_agents(self):
        # reviewer is auto-spawned (disable-model-invocation: true) — it must NOT
        # be advertised to the model, parity with skills.
        from agent_cli.prompts.system_prompt import build_agent_descriptions

        desc = build_agent_descriptions()
        assert "reviewer" not in desc
        assert "explorer" in desc  # normal agents still shown

    def test_includes_delegate_usage(self):
        from agent_cli.prompts.system_prompt import build_agent_descriptions

        desc = build_agent_descriptions()
        assert '"agent"' in desc  # nested key inside the task
        # Default format is multi-op now (Step 2): render_action_input flattens
        # the `delegate_` prefix to the flat op shape, so the top-level key is
        # the plain `"tasks"`, not the prefixed `"delegate_tasks"`.
        assert '"tasks"' in desc
        assert '"delegate_tasks"' not in desc

    def test_empty_when_no_agents(self, tmp_path, monkeypatch):
        from agent_cli.prompts.system_prompt import build_agent_descriptions
        import agent_cli.tools.delegate as delegate_mod

        delegate_mod._reset_agent_loader([tmp_path / "empty"])
        desc = build_agent_descriptions()
        assert desc == ""


class TestApplyStyle:
    def test_apply_style_none_no_change(self):
        from agent_cli.main import _apply_style

        old = get_renderer()
        _apply_style(None)
        assert get_renderer() is old

    def test_apply_style_minimal(self):
        from agent_cli.main import _apply_style

        old = get_renderer()
        _apply_style("minimal")
        assert isinstance(get_renderer(), MinimalRenderer)
        set_renderer(old)

    def test_apply_style_unknown_exits(self):
        """Removed bundled renderers (fancy/adaptive) — passing one of
        their names should now hit the dispatcher's "not found" path
        and exit cleanly via typer rather than crashing."""
        import pytest
        import typer
        from agent_cli.main import _apply_style

        with pytest.raises(typer.Exit):
            _apply_style("fancy")


class TestDispatchAgent:
    def test_dispatch_agent_not_found(self):
        from unittest.mock import MagicMock
        from agent_cli.main import _dispatch_agent, _AGENT_NOT_FOUND
        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=8192,
            max_output_tokens=2048,
            supports_structured_output=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        provider = MagicMock()

        result = _dispatch_agent(
            "@nonexistent_agent_xyz do something",
            provider,
            caps,
            "test",
            "openai",
            "http://127.0.0.1:8000/v1",
            "",
        )
        assert result is _AGENT_NOT_FOUND

    def test_dispatch_agent_no_task(self):
        from unittest.mock import MagicMock
        from agent_cli.main import _dispatch_agent, _AGENT_NOT_FOUND

        result = _dispatch_agent(
            "@",
            MagicMock(),
            None,
            "test",
            "openai",
            "http://127.0.0.1:8000/v1",
            "",
        )
        assert result is _AGENT_NOT_FOUND
