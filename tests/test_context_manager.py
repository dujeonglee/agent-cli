"""Tests for ContextManager (FIFO + history.jsonl)."""

import json

import pytest

from agent_cli.context.manager import ContextManager, _to_natural_language


# ── Fixtures ──────────────────────────────────────────


@pytest.fixture
def session_dir(tmp_path):
    return tmp_path / "sessions" / "test-session"


@pytest.fixture
def ctx(session_dir):
    return ContextManager(session_dir, max_context_tokens=10000)


@pytest.fixture
def wf():
    """ReAct wire-format plugin used by ``_to_natural_language`` tests.

    The function takes the plugin as a parameter so the assistant
    branch can delegate to ``render_assistant_from_history``. Tests
    against the plugin's behavior live in ``test_wire_formats_react``;
    here we only need a real plugin that produces the same string
    shapes the legacy free function used to."""
    from agent_cli.wire_formats import get as get_wire_format

    return get_wire_format("react")


# ── FIFO Behavior ─────────────────────────────────────


class TestFIFO:
    def test_add_and_get(self, ctx):
        ctx.add({"role": "user", "content": "hello"})
        ctx.add(
            {
                "role": "assistant",
                "thought": "greeting",
                "action": "complete",
                "action_input": {"result": "hi"},
            }
        )
        msgs = ctx.get_messages()
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    def test_fifo_eviction(self, session_dir):
        """When cache exceeds token budget, oldest messages are evicted."""
        # Each "msg-N" message is 5 tokens. Budget 25 holds 5, so 7 → 5.
        ctx = ContextManager(session_dir, max_context_tokens=25)
        for i in range(7):
            ctx.add({"role": "user", "content": f"msg-{i}"})
        msgs = ctx.get_raw_messages()
        assert len(msgs) == 5
        assert msgs[0]["content"] == "msg-2"
        assert msgs[-1]["content"] == "msg-6"

    def test_fifo_preserves_order(self, session_dir):
        """Eviction preserves chronological order."""
        # Each "u-N" message is 4 tokens. Budget 20 holds 5, so 6 → 5.
        ctx = ContextManager(session_dir, max_context_tokens=20)
        for i in range(6):
            ctx.add({"role": "user", "content": f"u{i}"})
        raw = ctx.get_raw_messages()
        assert len(raw) == 5
        assert raw[0]["content"] == "u1"  # u0 evicted

    def test_empty_session(self, ctx):
        assert ctx.get_messages() == []
        assert ctx.get_raw_messages() == []


# ── history.jsonl Persistence ─────────────────────────


class TestHistoryPersistence:
    def test_append_creates_file(self, ctx):
        ctx.add({"role": "user", "content": "first"})
        assert ctx.history_path.is_file()

    def test_append_only(self, ctx):
        ctx.add({"role": "user", "content": "msg1"})
        ctx.add({"role": "user", "content": "msg2"})
        lines = ctx.history_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["content"] == "msg1"
        assert json.loads(lines[1])["content"] == "msg2"

    def test_history_not_truncated_by_fifo(self, session_dir):
        """history.jsonl keeps all messages even when FIFO evicts."""
        # Each "msg-N" is 5 tokens. Budget 25 holds 5.
        ctx = ContextManager(session_dir, max_context_tokens=25)
        for i in range(10):
            ctx.add({"role": "user", "content": f"msg-{i}"})
        lines = ctx.history_path.read_text().strip().split("\n")
        assert len(lines) == 10  # All 10 messages persisted
        assert len(ctx.get_raw_messages()) == 5  # Only 5 in cache

    def test_unicode_content(self, ctx):
        ctx.add({"role": "user", "content": "한글 테스트 🚀"})
        lines = ctx.history_path.read_text().strip().split("\n")
        restored = json.loads(lines[0])
        assert restored["content"] == "한글 테스트 🚀"

    def test_append_recreates_session_dir_if_removed(self, session_dir):
        """If the session dir is wiped between ctx construction and the
        first write (external `rm -rf .agent-cli/sessions/`, parallel
        cleanup, etc.), ``_append_to_history`` must defensively
        re-mkdir rather than crash with FileNotFoundError. This guards
        the parallel delegate path where 8 worker contexts all share
        the parent session tree and a stale cleanup can race the first
        history flush."""
        import shutil

        ctx = ContextManager(session_dir, max_context_tokens=1000)
        assert session_dir.is_dir()
        # Simulate external rm -rf between ctx init and first write.
        shutil.rmtree(session_dir)
        assert not session_dir.is_dir()
        # First write should resurrect the dir + succeed without
        # raising. Without the defensive mkdir this would raise
        # FileNotFoundError on the open().
        ctx.add({"role": "user", "content": "post-rm"})
        assert (session_dir / "history.jsonl").is_file()
        lines = (session_dir / "history.jsonl").read_text().strip().split("\n")
        assert json.loads(lines[0])["content"] == "post-rm"


# ── Session Resume ────────────────────────────────────


class TestSessionResume:
    def test_resume_restores_cache(self, session_dir):
        # Write session
        ctx1 = ContextManager(session_dir, max_context_tokens=25)
        for i in range(3):
            ctx1.add({"role": "user", "content": f"msg-{i}"})

        # Resume session
        ctx2 = ContextManager(session_dir, max_context_tokens=25, resume=True)
        raw = ctx2.get_raw_messages()
        assert len(raw) == 3
        assert raw[0]["content"] == "msg-0"
        assert raw[2]["content"] == "msg-2"

    def test_resume_respects_token_budget(self, session_dir):
        """Resume only loads messages that fit within token budget."""
        ctx1 = ContextManager(session_dir, max_context_tokens=15)
        for i in range(10):
            ctx1.add({"role": "user", "content": f"msg-{i}"})

        ctx2 = ContextManager(session_dir, max_context_tokens=15, resume=True)
        raw = ctx2.get_raw_messages()
        assert len(raw) == 3
        assert raw[0]["content"] == "msg-7"
        assert raw[2]["content"] == "msg-9"

    def test_resume_continues_append(self, session_dir):
        """After resume, new messages append to existing history."""
        ctx1 = ContextManager(session_dir, max_context_tokens=25)
        ctx1.add({"role": "user", "content": "old"})

        ctx2 = ContextManager(session_dir, max_context_tokens=25, resume=True)
        ctx2.add({"role": "user", "content": "new"})

        lines = ctx2.history_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["content"] == "old"
        assert json.loads(lines[1])["content"] == "new"

    def test_resume_no_file(self, session_dir):
        """Resume with no history file starts empty."""
        ctx = ContextManager(session_dir, max_context_tokens=25, resume=True)
        assert ctx.get_raw_messages() == []

    def test_resume_handles_corrupt_lines(self, session_dir):
        """Corrupt JSON lines are skipped during restore."""
        session_dir.mkdir(parents=True, exist_ok=True)
        history_path = session_dir / "history.jsonl"
        history_path.write_text(
            '{"role":"user","content":"good"}\n'
            "NOT JSON\n"
            '{"role":"user","content":"also good"}\n'
        )
        ctx = ContextManager(session_dir, max_context_tokens=25, resume=True)
        raw = ctx.get_raw_messages()
        assert len(raw) == 2
        assert raw[0]["content"] == "good"
        assert raw[1]["content"] == "also good"


# ── Natural Language Conversion ───────────────────────


class TestNaturalLanguageConversion:
    def test_user_input(self, wf):
        msg = {"role": "user", "content": "인증 시스템을 리팩토링 해줘"}
        result = _to_natural_language(msg, wf)
        assert result["role"] == "user"
        assert result["content"] == "인증 시스템을 리팩토링 해줘"

    def test_assistant_tool_call(self, wf):
        msg = {
            "role": "assistant",
            "thought": "auth.py를 읽어 구조를 파악해야 한다",
            "action": "read_file",
            "action_input": {"path": "src/auth.py"},
        }
        result = _to_natural_language(msg, wf)
        assert result["role"] == "assistant"
        # ReAct round-trips structured fields back to the JSON wire
        # shape, so the model sees the same emission shape regardless
        # of whether the turn came from live buffer or history.
        parsed = json.loads(result["content"])
        assert parsed["thought"] == "auth.py를 읽어 구조를 파악해야 한다"
        assert parsed["action"] == "read_file"
        assert parsed["action_input"] == {"path": "src/auth.py"}

    def test_assistant_complete(self, wf):
        msg = {
            "role": "assistant",
            "thought": "모든 작업이 완료되었다",
            "action": "complete",
            "action_input": {"result": "JWT 리팩토링 완료"},
        }
        result = _to_natural_language(msg, wf)
        assert result["role"] == "assistant"
        # complete uses the same JSON wire shape — no special-case
        # natural-language formatting.
        parsed = json.loads(result["content"])
        assert parsed["action"] == "complete"
        assert parsed["action_input"]["result"] == "JWT 리팩토링 완료"

    def test_assistant_delegate(self, wf):
        msg = {
            "role": "assistant",
            "thought": "explorer에게 의존성 분석을 위임하겠다",
            "action": "delegate",
            "action_input": {
                "tasks": [{"task": "auth.py 의존성 조사", "agent": "explorer"}]
            },
        }
        result = _to_natural_language(msg, wf)
        parsed = json.loads(result["content"])
        assert parsed["action"] == "delegate"
        assert parsed["action_input"]["tasks"][0]["agent"] == "explorer"

    def test_assistant_shell(self, wf):
        msg = {
            "role": "assistant",
            "thought": "테스트를 실행하겠다",
            "action": "shell",
            "action_input": {"command": "pytest tests/ -v"},
        }
        result = _to_natural_language(msg, wf)
        parsed = json.loads(result["content"])
        assert parsed["action"] == "shell"
        assert parsed["action_input"]["command"] == "pytest tests/ -v"

    def test_assistant_run_skill(self, wf):
        msg = {
            "role": "assistant",
            "thought": "코드를 요약하겠다",
            "action": "run_skill",
            "action_input": {"name": "summarize", "arguments": "src/"},
        }
        result = _to_natural_language(msg, wf)
        parsed = json.loads(result["content"])
        assert parsed["action"] == "run_skill"
        assert parsed["action_input"]["name"] == "summarize"
        assert parsed["action_input"]["arguments"] == "src/"

    def test_observation_read_file(self, wf):
        msg = {
            "role": "user",
            "tool": "read_file",
            "args": {"path": "src/auth.py"},
            "content": "import hashlib\nclass AuthManager:\n    pass",
        }
        result = _to_natural_language(msg, wf)
        assert result["role"] == "user"
        assert "[read_file] src/auth.py" in result["content"]
        assert "import hashlib" in result["content"]

    def test_observation_with_artifact(self, wf):
        msg = {
            "role": "user",
            "tool": "delegate",
            "agent": "explorer",
            "content": "auth.py는 3곳에서 import됨",
            "artifact": "delegate_explorer_b7c1_20260405T143045567/",
        }
        result = _to_natural_language(msg, wf)
        assert "[delegate]" in result["content"]
        assert "auth.py는 3곳에서 import됨" in result["content"]
        assert "→ delegate_explorer_b7c1_20260405T143045567/" in result["content"]

    def test_observation_shell(self, wf):
        msg = {
            "role": "user",
            "tool": "shell",
            "args": {"command": "pytest tests/ -v"},
            "content": "12 passed, 1 failed",
        }
        result = _to_natural_language(msg, wf)
        assert "[shell]" in result["content"]
        assert "12 passed, 1 failed" in result["content"]

    def test_assistant_no_thought(self, wf):
        """Assistant record with action but no thought field — the
        re-emit substitutes an empty string for thought so the wire
        shape stays uniform across recoveries (3-field JSON object
        with empty defaults vs full content)."""
        msg = {
            "role": "assistant",
            "action": "read_file",
            "action_input": {"path": "test.py"},
        }
        result = _to_natural_language(msg, wf)
        parsed = json.loads(result["content"])
        assert parsed == {
            "thought": "",
            "action": "read_file",
            "action_input": {"path": "test.py"},
        }

    def test_assistant_plain_content(self, wf):
        """Fallback: assistant message with only content field."""
        msg = {"role": "assistant", "content": "plain response"}
        result = _to_natural_language(msg, wf)
        assert result["content"] == "plain response"


# ── Fork Support ──────────────────────────────────────


class TestFork:
    def test_fork_copies_history(self, session_dir, tmp_path):
        ctx = ContextManager(session_dir, max_context_tokens=25)
        ctx.add({"role": "user", "content": "parent msg 1"})
        ctx.add(
            {
                "role": "assistant",
                "thought": "ok",
                "action": "complete",
                "action_input": {"result": "done"},
            }
        )

        target = tmp_path / "delegate_coder_abc_123"
        copied_path = ctx.fork_history_to(target)

        assert copied_path.is_file()
        lines = copied_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["content"] == "parent msg 1"

    def test_fork_then_resume(self, session_dir, tmp_path):
        """Forked history can be used to create a new ContextManager."""
        ctx = ContextManager(session_dir, max_context_tokens=25)
        ctx.add({"role": "user", "content": "parent msg"})

        target = tmp_path / "delegate_test"
        ctx.fork_history_to(target)

        child = ContextManager(target, max_context_tokens=25, resume=True)
        assert len(child.get_raw_messages()) == 1
        assert child.get_raw_messages()[0]["content"] == "parent msg"

        # Child can append
        child.add({"role": "user", "content": "child msg"})
        assert len(child.get_raw_messages()) == 2

        # Parent unaffected
        assert len(ctx.get_raw_messages()) == 1

    def test_fork_empty_history(self, session_dir, tmp_path):
        """Fork with no history creates empty target."""
        ctx = ContextManager(session_dir, max_context_tokens=25)
        target = tmp_path / "delegate_empty"
        copied_path = ctx.fork_history_to(target)
        # File doesn't exist since source doesn't exist
        assert not copied_path.is_file()


# ── get_messages Integration ──────────────────────────


class TestGetMessagesIntegration:
    def test_full_conversation_flow(self, session_dir):
        """Simulate a realistic conversation and verify output."""
        ctx = ContextManager(session_dir, max_context_tokens=10000)
        ctx.add({"role": "user", "content": "auth.py를 리팩토링 해줘"})
        ctx.add(
            {
                "role": "assistant",
                "thought": "현재 구조를 파악하기 위해 auth.py를 읽겠다",
                "action": "read_file",
                "action_input": {"path": "src/auth.py"},
            }
        )
        ctx.add(
            {
                "role": "user",
                "tool": "read_file",
                "args": {"path": "src/auth.py"},
                "content": "class AuthManager:\n    pass",
            }
        )
        ctx.add(
            {
                "role": "assistant",
                "thought": "리팩토링이 완료되었다",
                "action": "complete",
                "action_input": {"result": "AuthManager 리팩토링 완료"},
            }
        )

        msgs = ctx.get_messages()
        assert len(msgs) == 4
        assert msgs[0] == {"role": "user", "content": "auth.py를 리팩토링 해줘"}
        # Assistant turns round-trip back to the JSON wire shape.
        parsed_call = json.loads(msgs[1]["content"])
        assert parsed_call["action"] == "read_file"
        assert parsed_call["action_input"] == {"path": "src/auth.py"}
        # Observation stays in natural-language ``[tool] args`` header form.
        assert "[read_file] src/auth.py" in msgs[2]["content"]
        # complete uses the same JSON wire shape.
        parsed_complete = json.loads(msgs[3]["content"])
        assert parsed_complete["action"] == "complete"
        assert parsed_complete["action_input"]["result"] == "AuthManager 리팩토링 완료"

    def test_artifact_paths_preserved_in_messages(self, ctx):
        """Artifact paths from observations appear in get_messages output."""
        ctx.add(
            {
                "role": "user",
                "tool": "delegate",
                "agent": "coder",
                "content": "구현 완료",
                "artifact": "delegate_coder_f1a9_20260405T143230456/",
            }
        )
        msgs = ctx.get_messages()
        assert "delegate_coder_f1a9_20260405T143230456/" in msgs[0]["content"]


# ── wire_format attachment ────────────────────────────


class TestWireFormatAttachment:
    """ContextManager owns a wire_format plugin per instance.

    H4 attaches a plugin to each ctx so ``get_messages()`` can route the
    history → message conversion through the plugin without the
    surrounding code having to thread the plugin through every call.
    Default fallback covers headless / test paths that don't pass one
    explicitly.
    """

    def test_default_falls_back_to_react(self, session_dir):
        from agent_cli.wire_formats.react import ReActFormat

        ctx = ContextManager(session_dir, max_context_tokens=1000)
        assert isinstance(ctx.wire_format, ReActFormat)

    def test_explicit_wire_format_is_kept(self, session_dir):
        from agent_cli.wire_formats import get as get_wire_format

        plugin = get_wire_format("react")
        ctx = ContextManager(session_dir, max_context_tokens=1000, wire_format=plugin)
        # Identity, not just equality — the same instance the caller
        # passed in must survive on the ctx.
        assert ctx.wire_format is plugin

    def test_custom_plugin_instance_attached_unchanged(self, session_dir):
        """A non-registered plugin (e.g. a test stand-in) is accepted.

        Pinning the contract that ContextManager doesn't validate the
        plugin against the registry — it just trusts what's passed and
        falls back only when ``None`` is given. Lets tests pass fakes
        without touching the global registry.
        """

        class _StubFormat:
            name = "_stub"

        stub = _StubFormat()
        ctx = ContextManager(session_dir, max_context_tokens=1000, wire_format=stub)
        assert ctx.wire_format is stub
