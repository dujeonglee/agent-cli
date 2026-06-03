"""Tests for context/session.py — project-local session persistence."""

from __future__ import annotations

import json

import pytest

import agent_cli.context.session as session_mod
from agent_cli.context.session import (
    create_session,
    load_session,
    recent_exchanges,
    save_meta,
)


@pytest.fixture(autouse=True)
def _use_tmp_sessions_dir(tmp_path, monkeypatch):
    """Redirect sessions base dir to temp for all tests."""
    monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path / ".agent-cli")


class TestCreateSession:
    def test_creates_with_defaults(self):
        meta = create_session("/tmp/workspace")
        assert meta.workspace == "/tmp/workspace"
        assert meta.session_id
        assert meta.updated_at

    def test_session_id_is_timestamp(self):
        meta = create_session()
        assert meta.session_id.isdigit()

    def test_no_workspace_hash(self):
        """New format: no workspace_hash field."""
        meta = create_session("/tmp/ws")
        assert not hasattr(meta, "workspace_hash") or meta.workspace_hash == ""

    def test_default_response_format_is_prefix_md(self):
        assert create_session().response_format == "prefix_md"

    def test_response_format_stored(self):
        meta = create_session("/tmp/ws", response_format="prefix_md")
        assert meta.response_format == "prefix_md"


class TestLoadSession:
    def test_load_existing(self, tmp_path):
        meta = create_session("/tmp/ws")
        save_meta(meta)
        loaded = load_session(meta.session_id)
        assert loaded is not None
        assert loaded.session_id == meta.session_id

    def test_load_nonexistent(self, tmp_path):
        assert load_session("999999999") is None

    def test_response_format_round_trips(self, tmp_path):
        meta = create_session("/tmp/ws", response_format="prefix_md")
        save_meta(meta)
        loaded = load_session(meta.session_id)
        assert loaded is not None
        assert loaded.response_format == "prefix_md"

    def test_legacy_session_defaults_to_prefix_md(self, tmp_path):
        """A session.jsonl written before the response_format field existed
        (no such key in _meta) loads with the current default (prefix_md);
        backward-compat to the old 'react' default is intentionally dropped."""
        sid = "1700000000"
        d = session_mod._SESSIONS_BASE / "sessions" / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "session.jsonl").write_text(
            json.dumps(
                {
                    "_meta": {
                        "session_id": sid,
                        "workspace": "/tmp",
                        "updated_at": "2026-01-01 00:00:00",
                        "query": "",
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        loaded = load_session(sid)
        assert loaded is not None
        assert loaded.response_format == "prefix_md"


class TestRecentExchanges:
    """Resume needs to surface the last N user↔assistant pairs from
    history.jsonl so the user can pick up without scrolling. Tool calls,
    observations, and intermediate thoughts are noise here — only the
    user's actual queries paired with the assistant's final answer
    matter."""

    def _write(self, path, entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
            encoding="utf-8",
        )

    def test_missing_file_returns_empty(self, tmp_path):
        assert recent_exchanges(tmp_path / "missing.jsonl") == []

    def test_simple_pair(self, tmp_path):
        path = tmp_path / "history.jsonl"
        self._write(
            path,
            [
                {"role": "user", "content": "Hi"},
                {
                    "role": "assistant",
                    "action": "complete",
                    "action_input": {"result": "Hello!"},
                },
            ],
        )
        assert recent_exchanges(path) == [("Hi", "Hello!")]

    def test_skips_tool_observations(self, tmp_path):
        """Observations look like role=user but they're tool results,
        not real queries. They must not pair with completes."""
        path = tmp_path / "history.jsonl"
        self._write(
            path,
            [
                {"role": "user", "content": "Run ls"},
                {
                    "role": "assistant",
                    "action": "shell",
                    "action_input": {"command": "ls"},
                },
                {"role": "user", "content": "Observation: file1\nfile2"},
                {
                    "role": "assistant",
                    "action": "complete",
                    "action_input": {"result": "Listed 2 files"},
                },
            ],
        )
        assert recent_exchanges(path) == [("Run ls", "Listed 2 files")]

    def test_skips_structured_tool_results(self, tmp_path):
        """role=user with a `tool` field is a tool result message —
        also not a real user query."""
        path = tmp_path / "history.jsonl"
        self._write(
            path,
            [
                {"role": "user", "content": "Read foo"},
                {
                    "role": "user",
                    "tool": "read_file",
                    "args": {"path": "foo"},
                    "content": "..file contents..",
                },
                {
                    "role": "assistant",
                    "action": "complete",
                    "action_input": {"result": "Done"},
                },
            ],
        )
        assert recent_exchanges(path) == [("Read foo", "Done")]

    def test_interrupted_query_marked(self, tmp_path):
        """A user query with no matching complete (e.g. user pressed
        Ctrl+C, or max_turns hit) closes as '(no completion)' so it's
        still visible on resume."""
        path = tmp_path / "history.jsonl"
        self._write(
            path,
            [
                {"role": "user", "content": "First Q"},
                {
                    "role": "assistant",
                    "action": "shell",
                    "action_input": {"command": "ls"},
                },
                {"role": "user", "content": "Second Q"},
                {
                    "role": "assistant",
                    "action": "complete",
                    "action_input": {"result": "Done with second"},
                },
            ],
        )
        assert recent_exchanges(path) == [
            ("First Q", "(no completion)"),
            ("Second Q", "Done with second"),
        ]

    def test_trailing_pending_at_eof(self, tmp_path):
        """A pending query at EOF (no complete after) also surfaces."""
        path = tmp_path / "history.jsonl"
        self._write(
            path,
            [
                {"role": "user", "content": "Only Q"},
                {
                    "role": "assistant",
                    "action": "shell",
                    "action_input": {"command": "ls"},
                },
            ],
        )
        assert recent_exchanges(path) == [("Only Q", "(no completion)")]

    def test_returns_only_last_n(self, tmp_path):
        path = tmp_path / "history.jsonl"
        entries = []
        for i in range(15):
            entries.append({"role": "user", "content": f"Q{i}"})
            entries.append(
                {
                    "role": "assistant",
                    "action": "complete",
                    "action_input": {"result": f"A{i}"},
                }
            )
        self._write(path, entries)
        result = recent_exchanges(path, n=10)
        assert len(result) == 10
        assert result[0] == ("Q5", "A5")
        assert result[-1] == ("Q14", "A14")

    def test_n_zero_returns_all(self, tmp_path):
        """n<=0 disables truncation — useful for callers that want the
        full pair list (tests, debug dumps)."""
        path = tmp_path / "history.jsonl"
        self._write(
            path,
            [
                {"role": "user", "content": "Q1"},
                {
                    "role": "assistant",
                    "action": "complete",
                    "action_input": {"result": "A1"},
                },
                {"role": "user", "content": "Q2"},
                {
                    "role": "assistant",
                    "action": "complete",
                    "action_input": {"result": "A2"},
                },
            ],
        )
        assert recent_exchanges(path, n=0) == [("Q1", "A1"), ("Q2", "A2")]

    def test_malformed_lines_skipped(self, tmp_path):
        """A corrupted JSON line in the middle must not abort the scan
        — just drop the bad line and keep going."""
        path = tmp_path / "history.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"role": "user", "content": "Q1"})
            + "\n"
            + "{not valid json\n"
            + "\n"  # blank line should also skip
            + json.dumps(
                {
                    "role": "assistant",
                    "action": "complete",
                    "action_input": {"result": "A1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        assert recent_exchanges(path) == [("Q1", "A1")]

    def test_complete_with_string_action_input(self, tmp_path):
        """Some legacy/edge runs emit action_input as a bare string
        instead of {result: ...}. Render that string as the answer."""
        path = tmp_path / "history.jsonl"
        self._write(
            path,
            [
                {"role": "user", "content": "Q"},
                {"role": "assistant", "action": "complete", "action_input": "bare A"},
            ],
        )
        assert recent_exchanges(path) == [("Q", "bare A")]

    def test_system_user_messages_filtered(self, tmp_path):
        """Loop-emitted role=user messages (retry hints, interrupt
        notice) must NOT pair with completes — they aren't real user
        input. The unified prefix list lives at
        ``agent_cli.wire_formats.all_system_user_prefixes()``; new
        system-injected prefixes from a wire-format plugin extend that
        list automatically."""
        from agent_cli.constants import INTERRUPT_NOTICE
        from agent_cli.wire_formats.react import ReActFormat

        # Use the plugin's static fallback messages directly so the
        # test exercises the actual production retry-hint strings.
        react = ReActFormat()
        retry_no_json = react.static_retry_hint_no_json()
        retry_no_action = react.static_retry_hint_no_action()

        path = tmp_path / "history.jsonl"
        self._write(
            path,
            [
                {"role": "user", "content": retry_no_json},
                {"role": "user", "content": "Real question"},
                {"role": "user", "content": retry_no_action},
                {
                    "role": "assistant",
                    "action": "complete",
                    "action_input": {"result": "Real answer"},
                },
                {"role": "user", "content": INTERRUPT_NOTICE},
            ],
        )
        # Only the real Q/A pair survives — both retry hints and the
        # interrupt notice are filtered out.
        assert recent_exchanges(path) == [("Real question", "Real answer")]

    def test_complete_without_pending_query_skipped(self, tmp_path):
        """A `complete` with no pending user query (e.g. corrupted file
        starting mid-stream) is dropped rather than producing a phantom
        pair."""
        path = tmp_path / "history.jsonl"
        self._write(
            path,
            [
                {
                    "role": "assistant",
                    "action": "complete",
                    "action_input": {"result": "phantom"},
                },
                {"role": "user", "content": "real Q"},
                {
                    "role": "assistant",
                    "action": "complete",
                    "action_input": {"result": "real A"},
                },
            ],
        )
        assert recent_exchanges(path) == [("real Q", "real A")]
