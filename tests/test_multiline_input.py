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


class TestReadRichInputDecodeError:
    """Bytes that fail UTF-8 decoding inside `input()` must NOT crash the
    CLI. Observed in the wild (session 1777073776) when the user pasted
    or IME-typed something that produced invalid UTF-8 mid-stream — the
    raw `UnicodeDecodeError` propagated up to the REPL/ask handler and
    aborted the session. The reader now catches it, prints one warning,
    and returns empty so the caller treats it as a missed line."""

    def _decode_err(self):
        # bytes 0xec at position 3 is the exact failure mode reported
        # in the bug — a Korean leading byte without continuation.
        return UnicodeDecodeError(
            "utf-8", b"abc\xec", 3, 4, "invalid continuation byte"
        )

    def test_first_line_decode_error_returns_empty(self):
        from agent_cli import input_history as ih
        from agent_cli.input_history import read_rich_input

        ih._decode_warning_shown = False  # reset for assertion below
        with patch("builtins.input", side_effect=self._decode_err()):
            result = read_rich_input("You: ")
        assert result == ""

    def test_first_line_decode_warning_only_shown_once(self, capsys):
        """Two consecutive decode errors → exactly one warning printed."""
        from agent_cli import input_history as ih
        from agent_cli.input_history import read_rich_input

        ih._decode_warning_shown = False
        with patch("builtins.input", side_effect=self._decode_err()):
            read_rich_input("You: ")
        first_err = capsys.readouterr().err
        assert "Input decode error" in first_err

        with patch("builtins.input", side_effect=self._decode_err()):
            read_rich_input("You: ")
        second_err = capsys.readouterr().err
        assert second_err == ""

    def test_multiline_continuation_decode_error_terminates_block(self):
        """If the decode error hits inside a triple-quote block, end the
        block with whatever was collected so far rather than crashing."""
        from agent_cli import input_history as ih
        from agent_cli.input_history import read_rich_input

        ih._decode_warning_shown = False
        inputs = ['"""', "first line", self._decode_err()]

        def fake_input(prompt=""):
            v = inputs.pop(0)
            if isinstance(v, UnicodeDecodeError):
                raise v
            return v

        with patch("builtins.input", side_effect=fake_input):
            result = read_rich_input("You: ")
        assert result == "first line"

    def test_ask_handler_survives_decode_error(self):
        """End-to-end: the ask handler in loop.py must not crash either —
        the previous bug surfaced specifically through `_handle_ask`."""
        from agent_cli import input_history as ih
        from agent_cli.loop import _handle_ask

        ih._decode_warning_shown = False
        with patch("builtins.input", side_effect=self._decode_err()):
            result = _handle_ask(["원하는 입력을 알려주세요"])
        # Empty answer reaches the model rather than a stack trace.
        assert "A: " in result
        assert "Q: 원하는 입력을 알려주세요" in result

    def test_main_repl_input_survives_decode_error(self):
        """Same guarantee for the main REPL wrapper."""
        from agent_cli import input_history as ih
        from agent_cli.main import _read_user_input

        ih._decode_warning_shown = False
        with patch("builtins.input", side_effect=self._decode_err()):
            result = _read_user_input("You: ")
        assert result == ""


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
