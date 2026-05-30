"""Tests for the shared ``@<agent>`` / ``/<skill>`` dispatcher.

The dispatcher (``agent_cli.main.try_dispatch_agent_or_skill``) is
called by BOTH the CLI chat REPL and the web worker thread; the only
difference is the ``DispatchOutput`` adapter each surface passes in.
This file pins the contract:

  1. Branch coverage — every (prefix, payload-shape) combination
     dispatches to the right ``DispatchOutput`` method.
  2. Adapter coverage — both ``_ConsoleDispatchOutput`` (CLI) and
     ``WebDispatchOutput`` (web) implement every Protocol method and
     produce surface-appropriate output.
  3. No-fall-through regression — unknown ``@``/`` /`` commands MUST
     NOT reach the LLM. A typo should never accidentally trigger a
     chat round-trip.

These are deliberately strict so a future "I'll just inline this one
small case" change to either chat REPL or web worker can't drift the
surfaces apart silently.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_cli.main import (
    _AGENT_NOT_FOUND,
    _SKILL_NOT_FOUND,
    DispatchOutput,
    _ConsoleDispatchOutput,
    try_dispatch_agent_or_skill,
)


# ── Recording adapter ───────────────────────────────────


class _RecordingOutput(DispatchOutput):
    """Captures every ``DispatchOutput`` call so tests can assert
    exactly which branch the dispatcher took, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def list_agents(self, names):
        self.calls.append(("list_agents", tuple(names)))

    def list_skills(self, skills):
        self.calls.append(("list_skills", tuple(sorted(skills.keys()))))

    def agent_not_found(self, name):
        self.calls.append(("agent_not_found", name))

    def agent_result(self, result):
        self.calls.append(("agent_result", result))

    def skill_not_found(self, name):
        self.calls.append(("skill_not_found", name))

    def skill_result(self, name, result):
        self.calls.append(("skill_result", name, result))


# ── Shared fixtures ─────────────────────────────────────


@pytest.fixture
def base_state(tmp_path):
    """Heavy state ``try_dispatch_agent_or_skill`` needs but doesn't
    inspect. Most tests want to assert on the dispatcher's *decisions*,
    not its parameter wiring — so a single common bag of dummies keeps
    test bodies focused."""
    from agent_cli.context.manager import ContextManager

    return {
        "llm_provider": MagicMock(),
        "capabilities": MagicMock(),
        "resolved_model": "test-model",
        "provider": "openai",
        "resolved_url": "http://localhost:11434",
        "resolved_key": "",
        "max_turns": 0,
        "verbose": False,
        "max_depth": 2,
        "delegate_timeout": 300,
        "ctx": ContextManager(session_dir=tmp_path),
        "session": None,
    }


# ── Dispatcher branch coverage ──────────────────────────


class TestDispatcherBranches:
    """Pin the prefix-pattern → ``DispatchOutput`` method mapping.

    Each test names the (prefix, shape) it covers in its docstring so
    a regression failure points directly at the broken branch.
    """

    @pytest.mark.parametrize("message", ["@", "@agents", "@somename"])
    def test_any_at_without_task_lists_agents(self, message, base_state, monkeypatch):
        """``@``, ``@agents``, and ``@<x>`` with no task all trigger
        the agent listing. CLI parity: typing ``@`` to discover what's
        available is a documented UX pattern (chat REPL help text)."""
        monkeypatch.setattr(
            "agent_cli.main._collect_agent_names", lambda: ["alpha", "beta"]
        )
        out = _RecordingOutput()
        handled = try_dispatch_agent_or_skill(message, out, **base_state)
        assert handled is True
        assert out.calls == [("list_agents", ("alpha", "beta"))]

    def test_at_name_with_task_dispatches_agent(self, base_state, monkeypatch):
        """``@<name> <task>`` reaches ``_dispatch_agent`` with the full
        original message and the resolved state."""
        captured = {}

        def fake_dispatch_agent(query, *args, **kwargs):
            captured["query"] = query
            captured["args_count"] = len(args)
            return "agent answer"

        monkeypatch.setattr("agent_cli.main._dispatch_agent", fake_dispatch_agent)

        out = _RecordingOutput()
        handled = try_dispatch_agent_or_skill(
            "@explorer find the bug",
            out,
            **base_state,
        )
        assert handled is True
        assert captured["query"] == "@explorer find the bug"
        assert out.calls == [("agent_result", "agent answer")]

    def test_at_unknown_agent_emits_not_found(self, base_state, monkeypatch):
        """``_dispatch_agent`` returning ``_AGENT_NOT_FOUND`` MUST
        surface as ``agent_not_found`` and the message MUST NOT fall
        through to the LLM (returns True)."""
        monkeypatch.setattr(
            "agent_cli.main._dispatch_agent",
            lambda *_a, **_kw: _AGENT_NOT_FOUND,
        )
        out = _RecordingOutput()
        handled = try_dispatch_agent_or_skill(
            "@nonexistent do something", out, **base_state
        )
        assert handled is True
        assert out.calls == [("agent_not_found", "nonexistent")]

    def test_slash_skills_lists_skills(self, base_state, monkeypatch):
        """``/skills`` is the listing twin of ``@agents`` — must hit
        ``list_skills`` before ``_dispatch_skill`` (which would return
        ``_SKILL_NOT_FOUND`` for the synthetic ``skills`` name)."""
        from agent_cli.skills.models import Skill

        fake = {
            "plan": Skill(name="plan", description="plan it", prompt_template=""),
        }
        monkeypatch.setattr("agent_cli.skills.load_skills", lambda: fake)
        # Wire ``_dispatch_skill`` to fail loudly if ever called for
        # ``/skills`` — that would be a routing bug.
        monkeypatch.setattr(
            "agent_cli.main._dispatch_skill",
            lambda *_a, **_kw: pytest.fail("/skills must not reach _dispatch_skill"),
        )
        out = _RecordingOutput()
        handled = try_dispatch_agent_or_skill("/skills", out, **base_state)
        assert handled is True
        assert out.calls == [("list_skills", ("plan",))]

    def test_slash_known_skill_dispatches(self, base_state, monkeypatch):
        """``/<known-skill> args`` reaches ``_dispatch_skill`` and emits
        ``skill_result`` with the answer."""
        captured = {}

        def fake_dispatch_skill(query, *args, **kwargs):
            captured["query"] = query
            return "skill output"

        monkeypatch.setattr("agent_cli.main._dispatch_skill", fake_dispatch_skill)

        out = _RecordingOutput()
        handled = try_dispatch_agent_or_skill(
            "/plan ship feature X",
            out,
            **base_state,
        )
        assert handled is True
        assert captured["query"] == "/plan ship feature X"
        assert out.calls == [("skill_result", "plan", "skill output")]

    def test_slash_unknown_command_emits_not_found(self, base_state, monkeypatch):
        """An unknown ``/<x>`` MUST emit ``skill_not_found`` AND return
        True so the message NEVER falls through to the LLM. This is the
        no-typo-to-LLM contract: ``/clera`` (a typo of ``/clear``)
        shouldn't ever become a chat turn."""
        monkeypatch.setattr(
            "agent_cli.main._dispatch_skill",
            lambda *_a, **_kw: _SKILL_NOT_FOUND,
        )
        out = _RecordingOutput()
        handled = try_dispatch_agent_or_skill("/clera", out, **base_state)
        assert handled is True
        assert out.calls == [("skill_not_found", "clera")]

    @pytest.mark.parametrize(
        "message",
        ["hello there", "how do I X?", "no prefix at all"],
    )
    def test_non_prefixed_messages_fall_through(self, message, base_state, monkeypatch):
        """Plain chat messages return False so the caller proceeds to
        the LLM. Dispatch helpers MUST NOT be invoked at all."""
        monkeypatch.setattr(
            "agent_cli.main._dispatch_agent",
            lambda *_a, **_kw: pytest.fail("must not dispatch on chat"),
        )
        monkeypatch.setattr(
            "agent_cli.main._dispatch_skill",
            lambda *_a, **_kw: pytest.fail("must not dispatch on chat"),
        )
        out = _RecordingOutput()
        handled = try_dispatch_agent_or_skill(message, out, **base_state)
        assert handled is False
        assert out.calls == []


# ── Session save behaviour ──────────────────────────────


class TestSessionPersistence:
    """Successful dispatches must persist ``session.query`` so a
    crash-resume reflects the last user intent; failed dispatches must
    NOT update it (so the prior valid query stays on record)."""

    def test_successful_agent_dispatch_saves_session_query(
        self, base_state, monkeypatch, tmp_path
    ):
        from agent_cli.context.session import SessionMeta

        session = SessionMeta(
            session_id="s1", workspace="/tmp", updated_at="now", query="prev"
        )
        base_state["session"] = session
        save_calls: list = []
        monkeypatch.setattr(
            "agent_cli.context.session.save_meta",
            lambda s: save_calls.append(s.query),
        )
        monkeypatch.setattr("agent_cli.main._dispatch_agent", lambda *a, **k: "ok")
        try_dispatch_agent_or_skill(
            "@explorer find X", _RecordingOutput(), **base_state
        )
        # Query truncated to 100 chars; whole short message preserved.
        assert session.query == "@explorer find X"
        assert save_calls == ["@explorer find X"]

    def test_not_found_does_not_save_session_query(self, base_state, monkeypatch):
        from agent_cli.context.session import SessionMeta

        session = SessionMeta(
            session_id="s1", workspace="/tmp", updated_at="now", query="prev"
        )
        base_state["session"] = session
        save_calls: list = []
        monkeypatch.setattr(
            "agent_cli.context.session.save_meta",
            lambda s: save_calls.append(s.query),
        )
        monkeypatch.setattr(
            "agent_cli.main._dispatch_agent",
            lambda *a, **k: _AGENT_NOT_FOUND,
        )
        try_dispatch_agent_or_skill("@ghost task", _RecordingOutput(), **base_state)
        assert session.query == "prev"
        assert save_calls == []


# ── _ConsoleDispatchOutput ──────────────────────────────


class TestConsoleDispatchOutput:
    """The CLI chat REPL is the only consumer; behaviour must match
    the pre-refactor output exactly so users notice nothing changed."""

    def _capture(self, fn) -> str:
        """Render ``fn()`` and return the plain text the user would see."""
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        recording = Console(file=buf, force_terminal=False, width=120)
        # Patch the module-level console for the duration of the call.
        import agent_cli.render as render_mod

        saved = render_mod.console
        render_mod.console = recording
        # Also patch the rebind in main.py — ``from .render import console``
        # captured a stale reference.
        import agent_cli.main as main_mod

        saved_main = main_mod.console
        main_mod.console = recording
        try:
            fn()
        finally:
            render_mod.console = saved
            main_mod.console = saved_main
        return buf.getvalue()

    def test_list_agents_empty(self):
        out = _ConsoleDispatchOutput()
        text = self._capture(lambda: out.list_agents([]))
        assert "No agents found." in text
        assert "Usage: @agent-name <task>" in text

    def test_list_agents_with_names(self):
        out = _ConsoleDispatchOutput()
        text = self._capture(lambda: out.list_agents(["alpha", "beta"]))
        assert "Available agents:" in text
        assert "@alpha" in text
        assert "@beta" in text

    def test_list_skills_filters_non_user_invocable(self):
        from agent_cli.skills.models import Skill

        skills = {
            "shown": Skill(
                name="shown",
                description="visible",
                prompt_template="",
                argument_hint="<args>",
                user_invocable=True,
            ),
            "hidden": Skill(
                name="hidden",
                description="should not appear",
                prompt_template="",
                user_invocable=False,
            ),
        }
        out = _ConsoleDispatchOutput()
        text = self._capture(lambda: out.list_skills(skills))
        assert "/shown <args>" in text
        assert "hidden" not in text

    def test_list_skills_empty_says_no_skills(self):
        out = _ConsoleDispatchOutput()
        text = self._capture(lambda: out.list_skills({}))
        assert "No skills found." in text

    def test_agent_not_found_prints_hint(self):
        out = _ConsoleDispatchOutput()
        text = self._capture(lambda: out.agent_not_found("ghost"))
        assert "Agent not found: @ghost" in text
        assert "Type @ to list" in text

    def test_agent_result_skips_when_none(self):
        out = _ConsoleDispatchOutput()
        text = self._capture(lambda: out.agent_result(None))
        assert text.strip() == ""

    def test_agent_result_prints_string(self):
        out = _ConsoleDispatchOutput()
        text = self._capture(lambda: out.agent_result("the answer"))
        assert "the answer" in text

    def test_skill_not_found_prints_hint(self):
        out = _ConsoleDispatchOutput()
        text = self._capture(lambda: out.skill_not_found("clera"))
        assert "Unknown command: /clera" in text
        assert "/help" in text

    def test_skill_result_none_shows_recovery_hints(self):
        out = _ConsoleDispatchOutput()
        text = self._capture(lambda: out.skill_result("plan", None))
        assert "/plan stopped without final answer" in text
        assert "/clear" in text
        assert "/quit" in text

    def test_skill_result_prints_string(self):
        out = _ConsoleDispatchOutput()
        text = self._capture(lambda: out.skill_result("plan", "done"))
        assert "done" in text


# ── WebDispatchOutput ───────────────────────────────────


class TestWebDispatchOutput:
    """Every ``DispatchOutput`` method must produce exactly one
    ``observation`` event on the WebRenderer. The test asserts both
    the event shape (so the frontend's existing observation card
    renderer handles it) and the content (so the user can read it).
    """

    def _make(self):
        from agent_cli.render.web import WebConnection, WebRenderer
        from agent_cli.web.server import WebDispatchOutput

        renderer = WebRenderer()
        conn = WebConnection(id="t")
        renderer.register_connection(conn)
        return WebDispatchOutput(renderer), conn

    def _pop(self, conn):
        event, data = conn.queue.get(timeout=1.0)
        return event, data

    def test_list_agents_empty(self):
        out, conn = self._make()
        out.list_agents([])
        event, data = self._pop(conn)
        assert event == "observation"
        assert data["tool_name"] == "agents"
        assert data["success"] is True
        assert "No agents found" in data["content"]

    def test_list_agents_with_names(self):
        out, conn = self._make()
        out.list_agents(["alpha", "beta"])
        event, data = self._pop(conn)
        assert event == "observation"
        assert "@alpha" in data["content"]
        assert "@beta" in data["content"]

    def test_list_skills_filters_non_user_invocable(self):
        from agent_cli.skills.models import Skill

        out, conn = self._make()
        out.list_skills(
            {
                "shown": Skill(
                    name="shown",
                    description="visible",
                    prompt_template="",
                    argument_hint="<args>",
                    user_invocable=True,
                ),
                "hidden": Skill(
                    name="hidden",
                    description="x",
                    prompt_template="",
                    user_invocable=False,
                ),
            }
        )
        event, data = self._pop(conn)
        assert event == "observation"
        assert "/shown" in data["content"]
        assert "hidden" not in data["content"]

    def test_list_skills_empty(self):
        out, conn = self._make()
        out.list_skills({})
        event, data = self._pop(conn)
        assert event == "observation"
        assert "No skills available" in data["content"]

    def test_agent_not_found_emits_failure_card(self):
        out, conn = self._make()
        out.agent_not_found("ghost")
        event, data = self._pop(conn)
        assert event == "observation"
        assert data["success"] is False
        assert "@ghost" in data["content"]

    def test_agent_result_is_silent(self):
        """``_dispatch_agent`` already streamed the answer through the
        renderer; emitting again would double-render in the chat."""
        out, conn = self._make()
        out.agent_result("anything")
        assert conn.queue.empty()

    def test_skill_not_found_emits_failure_card(self):
        out, conn = self._make()
        out.skill_not_found("clera")
        event, data = self._pop(conn)
        assert event == "observation"
        assert data["success"] is False
        assert "/clera" in data["content"]

    def test_skill_result_is_silent(self):
        out, conn = self._make()
        out.skill_result("plan", "done")
        out.skill_result("plan", None)
        assert conn.queue.empty()


# ── Protocol completeness ───────────────────────────────


class TestProtocolCompleteness:
    """Guard against a future ``DispatchOutput`` method addition that
    forgets to update one of the adapters — the surface drift the
    Protocol exists to prevent."""

    @pytest.fixture
    def protocol_methods(self) -> set[str]:
        return {
            name
            for name in dir(DispatchOutput)
            if not name.startswith("_") and callable(getattr(DispatchOutput, name))
        }

    def test_console_implements_every_protocol_method(self, protocol_methods):
        impl_methods = {
            name for name in dir(_ConsoleDispatchOutput) if not name.startswith("_")
        }
        missing = protocol_methods - impl_methods
        assert not missing, f"_ConsoleDispatchOutput missing methods: {missing}"

    def test_web_implements_every_protocol_method(self, protocol_methods):
        from agent_cli.web.server import WebDispatchOutput

        impl_methods = {
            name for name in dir(WebDispatchOutput) if not name.startswith("_")
        }
        missing = protocol_methods - impl_methods
        assert not missing, f"WebDispatchOutput missing methods: {missing}"


class TestStopEventThreading:
    """stop_event must reach both dispatch paths so the web Stop button
    can halt @agent (via tool_delegate) and /skill (via execute_skill)
    runs at a turn boundary — the plain chat run_loop is wired in the
    worker separately."""

    def test_threaded_to_dispatch_agent(self, base_state, monkeypatch):
        import threading

        captured = {}

        def fake_agent(query, *args, **kwargs):
            captured["stop_event"] = kwargs.get("stop_event")
            return "ok"

        monkeypatch.setattr("agent_cli.main._dispatch_agent", fake_agent)
        ev = threading.Event()
        try_dispatch_agent_or_skill(
            "@explorer find it", _RecordingOutput(), stop_event=ev, **base_state
        )
        assert captured["stop_event"] is ev

    def test_threaded_to_dispatch_skill(self, base_state, monkeypatch):
        import threading

        captured = {}

        def fake_skill(query, *args, **kwargs):
            captured["stop_event"] = kwargs.get("stop_event")
            return "ok"

        monkeypatch.setattr("agent_cli.main._dispatch_skill", fake_skill)
        ev = threading.Event()
        try_dispatch_agent_or_skill(
            "/plan do it", _RecordingOutput(), stop_event=ev, **base_state
        )
        assert captured["stop_event"] is ev
