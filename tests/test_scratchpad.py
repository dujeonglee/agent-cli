"""Tests for context/scratchpad — persistent context management."""

from __future__ import annotations

import pytest
from pathlib import Path

from agent_cli.context.scratchpad import (
    ArtifactMeta,
    ContextBudget,
    append_decision,
    append_progress,
    build_artifact_index,
    init_scratchpad,
    load_artifact,
    load_scratchpad,
    parse_frontmatter,
    render_frontmatter,
    save_artifact,
    save_scratchpad,
    select_artifacts,
)


@pytest.fixture
def tmp_agent_dir(tmp_path):
    """Create a temporary .agent-cli directory."""
    agent_dir = tmp_path / ".agent-cli"
    agent_dir.mkdir()
    (agent_dir / "artifacts").mkdir()
    return agent_dir


class TestFrontmatter:
    def test_parse_valid(self):
        text = "---\ntitle: hello\ntags: [a, b]\n---\n\nBody here"
        meta, body = parse_frontmatter(text)
        assert meta["title"] == "hello"
        assert meta["tags"] == ["a", "b"]
        assert body.strip() == "Body here"

    def test_parse_no_frontmatter(self):
        text = "Just plain markdown"
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_parse_invalid_yaml(self):
        text = "---\n: invalid: yaml: [\n---\n\nBody"
        meta, body = parse_frontmatter(text)
        assert meta == {}

    def test_roundtrip(self):
        meta = {"goal": "test", "tags": ["a", "b"]}
        body = "## Progress\n- step 1"
        rendered = render_frontmatter(meta, body)
        parsed_meta, parsed_body = parse_frontmatter(rendered)
        assert parsed_meta["goal"] == "test"
        assert parsed_meta["tags"] == ["a", "b"]
        assert "## Progress" in parsed_body


class TestContextBudget:
    def test_small_model(self):
        budget = ContextBudget.for_model(8192)
        assert budget.scratchpad_tokens > 0
        assert budget.artifact_tokens > 0
        assert budget.conversation_tokens > 0
        total_used = (
            budget.scratchpad_tokens
            + budget.artifact_tokens
            + budget.conversation_tokens
            + int(budget.total_tokens * budget.reserved_system)
            + int(budget.total_tokens * budget.reserved_response)
        )
        # Should not exceed total
        assert total_used <= budget.total_tokens * 1.05  # 5% margin for rounding

    def test_large_model(self):
        budget = ContextBudget.for_model(200000)
        # Large model should have higher artifact ratio
        small_budget = ContextBudget.for_model(8192)
        assert budget.artifact_ratio > small_budget.artifact_ratio

    def test_medium_model(self):
        budget = ContextBudget.for_model(32768)
        assert budget.scratchpad_tokens > 0

    def test_to_dict(self):
        budget = ContextBudget.for_model(128000)
        d = budget.to_dict()
        assert "total" in d
        assert "scratchpad" in d
        assert "artifacts" in d
        assert "conversation" in d


class TestScratchpad:
    def test_init_and_load(self, tmp_agent_dir):
        content = init_scratchpad("Build TX path", tmp_agent_dir)
        assert "Build TX path" in content

        loaded = load_scratchpad(tmp_agent_dir)
        assert "Build TX path" in loaded
        assert "## Progress" in loaded

    def test_load_nonexistent(self, tmp_agent_dir):
        assert load_scratchpad(tmp_agent_dir) == ""

    def test_save_and_load(self, tmp_agent_dir):
        save_scratchpad("# Test\nHello", tmp_agent_dir)
        assert load_scratchpad(tmp_agent_dir) == "# Test\nHello"

    def test_append_progress(self, tmp_agent_dir):
        init_scratchpad("Test goal", tmp_agent_dir)
        append_progress(1, "Analyzed file.c", "artifacts/turn_0001.md", tmp_agent_dir)
        content = load_scratchpad(tmp_agent_dir)
        assert "[턴1]" in content
        assert "Analyzed file.c" in content
        assert "artifacts/turn_0001.md" in content

    def test_append_decision(self, tmp_agent_dir):
        init_scratchpad("Test goal", tmp_agent_dir)
        append_decision(3, "Use pre-allocated pool", tmp_agent_dir)
        content = load_scratchpad(tmp_agent_dir)
        assert "[턴3]" in content
        assert "Use pre-allocated pool" in content

    def test_multiple_progress_entries(self, tmp_agent_dir):
        init_scratchpad("Test goal", tmp_agent_dir)
        append_progress(1, "Step one", base=tmp_agent_dir)
        append_progress(2, "Step two", base=tmp_agent_dir)
        content = load_scratchpad(tmp_agent_dir)
        assert "[턴1]" in content
        assert "[턴2]" in content


class TestArtifacts:
    def test_save_and_load(self, tmp_agent_dir):
        path = save_artifact(
            turn=1,
            content="## Analysis\nFound 3 functions.",
            tags=["tx", "analysis"],
            summary="TX path analysis",
            base=tmp_agent_dir,
        )
        assert Path(path).is_file()

        meta, body = load_artifact(path)
        assert meta.entry_id == "turn_0001"
        assert meta.turn == 1
        assert "tx" in meta.tags
        assert meta.summary == "TX path analysis"
        assert "Found 3 functions" in body

    def test_load_nonexistent(self):
        meta, body = load_artifact("/nonexistent/path.md")
        assert meta.entry_id == ""
        assert body == ""

    def test_build_index(self, tmp_agent_dir):
        save_artifact(1, "Content 1", ["a"], "Summary 1", tmp_agent_dir)
        save_artifact(2, "Content 2", ["b"], "Summary 2", tmp_agent_dir)
        save_artifact(3, "Content 3", ["a", "c"], "Summary 3", tmp_agent_dir)

        index = build_artifact_index(tmp_agent_dir)
        assert len(index) == 3
        assert index[0].turn == 1
        assert index[2].turn == 3

    def test_build_index_empty(self, tmp_agent_dir):
        index = build_artifact_index(tmp_agent_dir)
        assert index == []


class TestSelectArtifacts:
    def _make_index(self, n: int) -> list[ArtifactMeta]:
        return [
            ArtifactMeta(
                entry_id=f"turn_{i:04d}",
                turn=i,
                tags=[f"tag{i % 3}"],
                summary=f"Summary {i}",
                token_count=100,
                path=f"artifacts/turn_{i:04d}.md",
            )
            for i in range(1, n + 1)
        ]

    def test_recent_first(self):
        index = self._make_index(10)
        selected = select_artifacts(index, [], budget_tokens=500, recent_n=3)
        turns = [s.turn for s in selected]
        assert 10 in turns
        assert 9 in turns
        assert 8 in turns

    def test_tag_matching(self):
        index = self._make_index(10)
        # tag0 appears on turns 3, 6, 9 (i % 3 == 0)
        selected = select_artifacts(index, ["tag0"], budget_tokens=1000, recent_n=0)
        assert len(selected) > 0
        for s in selected:
            assert "tag0" in s.tags

    def test_budget_limit(self):
        index = self._make_index(10)
        # Each is 100 tokens, budget 250 = only 2 fit
        selected = select_artifacts(index, [], budget_tokens=250, recent_n=5)
        assert len(selected) == 2

    def test_empty_index(self):
        selected = select_artifacts([], ["tag"], budget_tokens=1000)
        assert selected == []


class TestContextManagerScratchpad:
    """Test ContextManager with scratchpad enabled."""

    @pytest.fixture
    def caps(self):
        from agent_cli.providers.compat import ModelCapabilities

        return ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=False,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

    @pytest.fixture
    def mock_provider(self):
        from unittest.mock import MagicMock
        from agent_cli.providers.base import LLMResponse

        provider = MagicMock()
        provider.call.return_value = LLMResponse(
            content="## Goal\nTest\n## Progress\nDone"
        )
        return provider

    def test_scratchpad_always_active(self, mock_provider, caps, tmp_agent_dir):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(
            mock_provider,
            "test",
            caps,
            scratchpad_dir=tmp_agent_dir,
        )
        assert ctx._budget is not None
        info = ctx.begin_turn("test")
        assert info["scratchpad_loaded"] is True

    def test_init_task(self, mock_provider, caps, tmp_agent_dir):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(
            mock_provider,
            "test",
            caps,
            scratchpad_dir=tmp_agent_dir,
        )
        ctx.init_task("Analyze TX path")
        content = load_scratchpad(tmp_agent_dir)
        assert "Analyze TX path" in content

    def test_end_turn_saves_artifact(self, mock_provider, caps, tmp_agent_dir):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(
            mock_provider,
            "test",
            caps,
            scratchpad_dir=tmp_agent_dir,
        )
        ctx.init_task("Test task")
        ctx.begin_turn("analyze something")

        path = ctx.end_turn(
            content="## Analysis\n" + "Detailed findings " * 20,
            tags=["analysis"],
            summary="Found important things",
        )
        assert path is not None
        assert Path(path).is_file()

        # Check scratchpad was updated
        sp = load_scratchpad(tmp_agent_dir)
        assert "Found important things" in sp

    def test_end_turn_with_decision(self, mock_provider, caps, tmp_agent_dir):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(
            mock_provider,
            "test",
            caps,
            scratchpad_dir=tmp_agent_dir,
        )
        ctx.init_task("Test task")
        ctx.begin_turn("decide something")

        ctx.end_turn(
            content="Compared approaches",
            summary="Evaluated options",
            decision="Use approach A over B",
        )

        sp = load_scratchpad(tmp_agent_dir)
        assert "Use approach A over B" in sp

    def test_scratchpad_injected_in_messages(self, mock_provider, caps, tmp_agent_dir):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(
            mock_provider,
            "test",
            caps,
            scratchpad_dir=tmp_agent_dir,
        )
        ctx.init_task("Test task")
        ctx.add("user", "hello")

        msgs = ctx.get_messages()
        # First message should be scratchpad injection
        assert any("[Scratchpad" in m.get("content", "") for m in msgs)

    def test_get_budget_info(self, mock_provider, caps, tmp_agent_dir):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(
            mock_provider,
            "test",
            caps,
            scratchpad_dir=tmp_agent_dir,
        )
        info = ctx.get_budget_info()
        assert info["mode"] == "scratchpad"
        assert "budget" in info

    def test_budget_info_always_scratchpad(self, mock_provider, caps, tmp_agent_dir):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(mock_provider, "test", caps, scratchpad_dir=tmp_agent_dir)
        info = ctx.get_budget_info()
        assert info["mode"] == "scratchpad"
        assert "budget" in info
