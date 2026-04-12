"""Tests for multiline input support (_read_user_input)."""

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
