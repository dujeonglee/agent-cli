"""Tests for multiline input support.

Covers both the shared reader (`input_history.read_rich_input`) and its
thin wrappers (`main._read_user_input`, `loop._handle_ask`).
"""

from __future__ import annotations

from unittest.mock import patch


class TestReadUserInput:
    def test_single_line(self):
        """Normal single line input works as before."""
        from agent_cli.main import _read_user_input

        with patch("builtins.input", return_value="hello"):
            with patch("select.select", return_value=([], [], [])):
                result = _read_user_input("You: ")
        assert result == "hello"

    def test_empty_input(self):
        """Empty input returns empty string."""
        from agent_cli.main import _read_user_input

        with patch("builtins.input", return_value="  "):
            result = _read_user_input("You: ")
        assert result == ""

    def test_explicit_multiline(self):
        """Triple-quote delimited multiline input."""
        from agent_cli.main import _read_user_input

        inputs = iter(['"""', "line one", "line two", '"""'])
        with patch("builtins.input", side_effect=inputs):
            result = _read_user_input("You: ")
        assert result == "line one\nline two"

    def test_explicit_multiline_with_empty_lines(self):
        """Triple-quote multiline preserves empty lines."""
        from agent_cli.main import _read_user_input

        inputs = iter(['"""', "first", "", "third", '"""'])
        with patch("builtins.input", side_effect=inputs):
            result = _read_user_input("You: ")
        assert result == "first\n\nthird"

    def test_paste_detection(self):
        """Paste detection reads buffered lines from stdin."""
        import io

        from agent_cli.main import _read_user_input

        fake_stdin = io.StringIO("second line\nthird line\n")

        # select returns stdin as readable for 2 calls, then empty
        select_results = [([fake_stdin], [], []), ([fake_stdin], [], []), ([], [], [])]

        with patch("builtins.input", return_value="first line"):
            with patch("select.select", side_effect=select_results):
                with patch("sys.stdin", fake_stdin):
                    result = _read_user_input("You: ")

        assert result == "first line\nsecond line\nthird line"

    def test_paste_detection_not_supported(self):
        """Falls back to single line if select raises."""
        from agent_cli.main import _read_user_input

        with patch("builtins.input", return_value="single"):
            with patch("select.select", side_effect=OSError("not supported")):
                result = _read_user_input("You: ")
        assert result == "single"


class TestReadRichInputDirect:
    """Exercise the shared reader on its own so we aren't relying on the
    main.py wrapper to notice regressions."""

    def test_single_quote_triple_heredoc(self):
        """''' ... ''' also opens multiline (mirrors \"\"\" behavior)."""
        from agent_cli.input_history import read_rich_input

        inputs = iter(["'''", "a", "b", "'''"])
        with patch("builtins.input", side_effect=inputs):
            result = read_rich_input("You: ")
        assert result == "a\nb"

    def test_custom_continuation_prompt(self):
        """continuation= is passed to the inner input() calls.

        Ask prompts inside a nested skill block need a depth-prefixed
        continuation so `... ` lines up under the │ gutter.
        """
        from agent_cli.input_history import read_rich_input

        seen_prompts: list[str] = []

        def fake_input(prompt=""):
            seen_prompts.append(prompt)
            remaining = fake_input.queue.pop(0)
            return remaining

        fake_input.queue = ['"""', "body", '"""']

        with patch("builtins.input", side_effect=fake_input):
            read_rich_input("MAIN> ", continuation="│ ... ")

        assert seen_prompts[0] == "MAIN> "
        assert all(p == "│ ... " for p in seen_prompts[1:])


class TestAskHandlerMultiline:
    """/ask inside a skill must accept the same multiline syntax as the
    top-level REPL — the bug was that _handle_ask called input() directly
    and ignored the shared reader."""

    def test_ask_accepts_triple_quote_multiline(self):
        from agent_cli.loop import _handle_ask

        inputs = iter(['"""', "line one", "line two", '"""'])
        with patch("builtins.input", side_effect=inputs):
            result = _handle_ask(["What should the skill do?"])

        assert "A: line one\nline two" in result
        assert "Q: What should the skill do?" in result

    def test_ask_single_line_still_works(self):
        from agent_cli.loop import _handle_ask

        with patch("builtins.input", return_value="simple answer"):
            with patch("select.select", return_value=([], [], [])):
                result = _handle_ask(["Anything?"])

        assert "A: simple answer" in result
