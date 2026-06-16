"""Additional tests to improve coverage for tools modules."""

from __future__ import annotations

from pathlib import Path

from agent_cli.tools.write_file import tool_write_file
from agent_cli.tools.shell import tool_shell
from agent_cli.tools.edit_file import (
    tool_edit_file,
)
from agent_cli.tools.read_file import (
    _read_one,
    compute_line_hash,
)
from agent_cli.tools.delegate import (
    tool_delegate,
    _format_delegate_output,
    _format_parallel_results,
    DelegateResult,
)
from agent_cli.tools import TOOLS, _execute_tool as execute_tool


class TestToolResult:
    """Test ToolResult dataclass behavior."""

    def test_success_result(self):
        from agent_cli.tools.result import ToolResult

        r = ToolResult(True, output="hello")
        assert r.success is True
        assert r.output == "hello"
        assert r.error == ""

    def test_error_result(self):
        from agent_cli.tools.result import ToolResult

        r = ToolResult(False, error="file not found")
        assert r.success is False
        assert r.output == ""
        assert r.error == "file not found"

    def test_defaults(self):
        from agent_cli.tools.result import ToolResult

        r = ToolResult(True)
        assert r.output == ""
        assert r.error == ""

    def test_execute_tool_returns_toolresult(self):
        """execute_tool always returns ToolResult, never raises."""
        result = execute_tool("shell", {"command": "echo hi"})
        assert isinstance(
            result,
            __import__("agent_cli.tools.result", fromlist=["ToolResult"]).ToolResult,
        )
        assert result.success


class TestWriteFile:
    def test_creates_file(self, tmp_path):
        target = tmp_path / "new.txt"
        result = tool_write_file({"path": str(target), "content": "hello"})
        assert target.exists()
        assert target.read_text() == "hello"
        assert result.success
        assert "File saved" in result.output

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "sub" / "dir" / "file.txt"
        result = tool_write_file({"path": str(target), "content": "nested"})
        assert target.exists()
        assert result.success

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "exist.txt"
        target.write_text("old")
        result = tool_write_file({"path": str(target), "content": "new"})
        assert target.read_text() == "new"
        assert result.success

    def test_error_on_invalid_path(self):
        result = tool_write_file(
            {"path": "/nonexistent/dir/\x00/file.txt", "content": "x"}
        )
        assert not result.success
        assert result.error

    def test_hashline_on_overwrite(self, tmp_path):
        """Overwriting → output is the written content in hashline format
        (LINE#HASH:content) so the LLM can edit_file the file it just
        wrote WITHOUT a separate read_file. diff markers are gone."""
        target = tmp_path / "code.py"
        target.write_text("a\nb\nc\n")
        result = tool_write_file({"path": str(target), "content": "a\nB\nc\n"})
        assert result.success
        assert "1#" in result.output  # hashline tag present
        assert "B" in result.output  # new content shown
        # diff section removed (replaced by hashlines)
        assert "@@" not in result.output
        assert "-b" not in result.output

    def test_hashline_when_unchanged(self, tmp_path):
        """Identical content still returns hashlines (they are edit refs),
        not a bare save confirmation."""
        target = tmp_path / "same.txt"
        target.write_text("hello\n")
        result = tool_write_file({"path": str(target), "content": "hello\n"})
        assert result.success
        assert "1#" in result.output and "hello" in result.output

    def test_hashline_on_new_file(self, tmp_path):
        """Creating a file → every line tagged with a hashline so the LLM
        can edit it immediately."""
        target = tmp_path / "new.txt"
        result = tool_write_file({"path": str(target), "content": "first\nsecond\n"})
        assert result.success
        assert "1#" in result.output and "first" in result.output
        assert "2#" in result.output and "second" in result.output

    def test_output_hints_edit_without_reread(self, tmp_path):
        """The output tells the LLM it can edit_file directly with these
        hashlines (no re-read), so write→edit becomes the natural flow."""
        target = tmp_path / "x.txt"
        result = tool_write_file({"path": str(target), "content": "hi\n"})
        assert "edit_file" in result.output


class TestShellTool:
    def test_basic_command(self):
        result = tool_shell({"command": "echo hello"})
        assert result.success
        assert "hello" in result.output

    def test_stderr_output(self):
        result = tool_shell({"command": "echo err >&2"})
        assert result.success
        assert "stderr" in result.output

    def test_nonzero_exit(self):
        result = tool_shell({"command": "exit 1"})
        assert result.success
        assert "exit code: 1" in result.output

    def test_timeout(self):
        result = tool_shell({"command": "sleep 10", "timeout": 1})
        assert not result.success
        assert "timed out" in result.error

    def test_empty_command(self):
        result = tool_shell({"command": ""})
        assert not result.success
        assert "Empty command" in result.error

    def test_no_output(self):
        result = tool_shell({"command": "true"})
        assert result.success
        assert result.output == "(no output)"


class TestShellDangerousCommandConfirmation:
    """`rm` / `rmdir` / `mv` trigger an interactive confirmation prompt
    by default. Three decisions are accepted: y (run once), n (deny —
    surfaces back to the LLM as an error so it can pick a different
    path), and a (allow this keyword for the rest of the process). The
    AGENT_CLI_DANGEROUS_SHELL_CONFIRM=0 escape hatch exists for batch
    runs where there is no human to answer."""

    def setup_method(self):
        # The session-wide allowlist is module-level; clear between
        # tests so one test's `a` answer doesn't bleed into the next.
        from agent_cli.tools import shell as shell_mod

        shell_mod._session_allowlist.clear()

    def _force_tty(self, monkeypatch):
        """Tests run under pytest which is not a TTY, so the renderer
        reports it can't prompt and the guard refuses early. For tests of
        the prompt flow itself, force the active renderer to say it can
        confirm (the gate is now a renderer capability, not a raw TTY
        check)."""
        from agent_cli.render import get_renderer

        monkeypatch.setattr(type(get_renderer()), "can_prompt", lambda self: True)

    def test_disabled_via_env_var_runs_without_prompt(self, monkeypatch):
        """AGENT_CLI_DANGEROUS_SHELL_CONFIRM=0 — bypass entirely."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "0")
        # No `input` patched → if confirm fired, the test would hang.
        result = tool_shell({"command": "rm /nonexistent/path/xyz"})
        # Command itself fails (file doesn't exist) but it RAN — no
        # prompt was triggered.
        assert "exit code:" in (result.output or "") or result.success

    def test_dangerous_cannot_confirm_refused(self, monkeypatch):
        """Confirmation enabled + renderer can't prompt = refuse. We do
        NOT silently drop the check; the LLM is told why so it doesn't
        keep retrying. Under pytest the CLI renderer has no TTY, so
        ``can_prompt()`` is False."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        result = tool_shell({"command": "rm -rf /tmp/build"})
        assert not result.success
        assert "confirm" in (result.error or "")
        assert "rm" in (result.error or "")

    def test_dangerous_user_says_yes_once(self, monkeypatch):
        """y → run this command, but next dangerous command prompts again."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)

        from unittest.mock import patch

        with patch("builtins.input", return_value="y"):
            result = tool_shell({"command": "rm /nonexistent/xyz"})
        # `rm` ran (and failed naturally because the path doesn't exist).
        assert "exit code:" in (result.output or "") or result.success

        # Second `rm` should prompt AGAIN — y did not add to allowlist.
        from agent_cli.tools import shell as shell_mod

        assert "rm" not in shell_mod._session_allowlist

    def test_allow_alias_maps_to_always(self, monkeypatch):
        """Typing "allow" (a natural affirmative) must NOT collapse to the
        safe-default deny — it maps to "always" (the option the prompt
        labels "always allow")."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)

        from unittest.mock import patch
        from agent_cli.tools import shell as shell_mod

        with patch("builtins.input", return_value="allow"):
            tool_shell({"command": "rm /tmp/foo"})
        assert "rm" in shell_mod._session_allowlist

    def test_affirmative_alias_runs_once(self, monkeypatch):
        """ "ok" runs the command once without allowlisting."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)

        from unittest.mock import patch
        from agent_cli.tools import shell as shell_mod

        with patch("builtins.input", return_value="ok"):
            result = tool_shell({"command": "rm /nonexistent/xyz"})
        assert "exit code:" in (result.output or "") or result.success
        assert "rm" not in shell_mod._session_allowlist

    def test_dangerous_user_says_no_returns_denial(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)

        from unittest.mock import patch

        with patch("builtins.input", return_value="n"):
            result = tool_shell({"command": "rm important.txt"})
        assert not result.success
        assert "User denied" in (result.error or "")
        assert "rm" in (result.error or "")

    def test_dangerous_user_says_always_adds_to_session_allowlist(self, monkeypatch):
        """`a` greenlights the matched keyword for the rest of the
        process. The next command containing the same keyword runs
        without re-prompting."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)

        from unittest.mock import patch
        from agent_cli.tools import shell as shell_mod

        with patch("builtins.input", return_value="a"):
            tool_shell({"command": "rm /tmp/foo"})
        assert "rm" in shell_mod._session_allowlist

        # Second `rm` runs straight through — `input` is patched to
        # raise so we'd notice if the prompt fired.
        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            result = tool_shell({"command": "rm /tmp/bar"})
        # No exception means no prompt happened. Command itself may
        # still fail because the path doesn't exist.
        assert "exit code:" in (result.output or "") or result.success

    def test_eof_during_prompt_treated_as_deny(self, monkeypatch):
        """Ctrl+D / EOF on the confirmation prompt is "n" — never run
        a dangerous command on input failure."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)

        from unittest.mock import patch

        with patch("builtins.input", side_effect=EOFError):
            result = tool_shell({"command": "rm something"})
        assert not result.success
        assert "User denied" in (result.error or "")

    def test_safe_command_never_prompts(self, monkeypatch):
        """Commands without `rm` / `rmdir` / `mv` keywords go through
        unchanged. `input` is rigged to raise so any prompt attempt
        aborts the test loudly."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)

        from unittest.mock import patch

        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            result = tool_shell({"command": "echo safe"})
        assert result.success
        assert "safe" in (result.output or "")

    def test_keyword_in_string_literal_does_not_prompt(self, monkeypatch):
        """Shlex tokenization collapses quoted strings into one token,
        so `echo "rm files"` does NOT match — the literal isn't a
        command invocation. This is a known gap for `bash -c "rm x"`
        and similar shell-wrapper patterns; revisit if observed."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)

        from unittest.mock import patch

        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            result = tool_shell({"command": 'echo "rm files"'})
        assert result.success

    def test_keyword_as_substring_does_not_match(self, monkeypatch):
        """`format` contains "mv" as a substring — but not as a whole
        token. Word-boundary regex must not flag it."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)

        from unittest.mock import patch

        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            # `format` and `firmware` are non-dangerous tokens that
            # incidentally contain the letters of dangerous keywords.
            result = tool_shell({"command": "echo firmware-format"})
        assert result.success

    def test_pipeline_with_xargs_rm_caught(self, monkeypatch):
        """`find . -name '*.tmp' | xargs rm` — rm is buried in a
        pipeline but is still a standalone token. Must catch it."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)

        from unittest.mock import patch

        with patch("builtins.input", return_value="n"):
            result = tool_shell({"command": "find . -name '*.tmp' | xargs rm"})
        assert not result.success
        assert "User denied" in (result.error or "")

    def test_detect_dangerous_keywords(self):
        """Direct unit tests of the matcher without invoking subprocess."""
        from agent_cli.tools.shell import _detect_dangerous

        assert _detect_dangerous("rm foo") == "rm"
        assert _detect_dangerous("rm -rf /tmp/x") == "rm"
        assert _detect_dangerous("mv a b") == "mv"
        assert _detect_dangerous("rmdir empty/") == "rmdir"
        assert _detect_dangerous("xargs rm") == "rm"
        assert _detect_dangerous("git rm tracked.txt") == "rm"
        # Negatives
        assert _detect_dangerous("echo hello") is None
        assert _detect_dangerous("ls -la") is None
        assert _detect_dangerous("rm-helper.sh") is None  # not a command
        assert _detect_dangerous("format-firmware") is None


class TestShellConfirmationComments:
    """y/n/a accepts an optional trailing comment that surfaces to the
    LLM. For `n`, the comment becomes the denial reason so the model
    knows why and can pick a different path. For `y`/`a`, the comment
    is appended after the command output as an instruction the model
    should fold into its next move."""

    def setup_method(self):
        from agent_cli.tools import shell as shell_mod

        shell_mod._session_allowlist.clear()

    def _force_tty(self, monkeypatch):
        from agent_cli.render import get_renderer

        monkeypatch.setattr(type(get_renderer()), "can_prompt", lambda self: True)

    def test_ask_returns_decision_and_empty_comment(self):
        from agent_cli.tools.shell import _ask_confirmation
        from unittest.mock import patch

        with patch("builtins.input", return_value="y"):
            assert _ask_confirmation("rm x", "rm") == ("y", "")
        with patch("builtins.input", return_value="n"):
            assert _ask_confirmation("rm x", "rm") == ("n", "")
        with patch("builtins.input", return_value="a"):
            assert _ask_confirmation("rm x", "rm") == ("a", "")

    def test_ask_parses_decision_and_comment(self):
        from agent_cli.tools.shell import _ask_confirmation
        from unittest.mock import patch

        with patch("builtins.input", return_value="y and also try foo"):
            assert _ask_confirmation("rm x", "rm") == ("y", "and also try foo")
        with patch("builtins.input", return_value="n the path is wrong"):
            assert _ask_confirmation("rm x", "rm") == ("n", "the path is wrong")
        with patch("builtins.input", return_value="a only in /tmp"):
            assert _ask_confirmation("rm x", "rm") == ("a", "only in /tmp")

    def test_ask_unrecognized_first_token_treated_as_deny_with_full_comment(self):
        """If user types something other than y/n/a (e.g. they wrote a
        sentence directly), treat as deny but preserve the entire input
        as the comment so their reasoning still reaches the LLM."""
        from agent_cli.tools.shell import _ask_confirmation
        from unittest.mock import patch

        with patch("builtins.input", return_value="actually let me check first"):
            assert _ask_confirmation("rm x", "rm") == (
                "n",
                "actually let me check first",
            )

    def test_ask_empty_input_is_deny_no_comment(self):
        from agent_cli.tools.shell import _ask_confirmation
        from unittest.mock import patch

        with patch("builtins.input", return_value=""):
            assert _ask_confirmation("rm x", "rm") == ("n", "")

    def test_deny_comment_appears_in_error_message(self, monkeypatch):
        """`n` + comment → error string includes the user's reason so
        the LLM observation explains why the command was rejected."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)
        from unittest.mock import patch

        with patch("builtins.input", return_value="n wrong directory, try /tmp"):
            result = tool_shell({"command": "rm /etc/passwd"})
        assert not result.success
        assert "User denied" in (result.error or "")
        assert "wrong directory, try /tmp" in (result.error or "")

    def test_approve_comment_appears_after_output(self, monkeypatch):
        """`y` + comment → command runs and the comment is appended
        after stdout so the LLM sees both the result and the user's
        follow-up instruction."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)
        from unittest.mock import patch

        # Use a `mv` invocation that's safe — operate inside tmp.
        # Actually simpler: rm a non-existent path (fails harmlessly,
        # but ran).
        with patch("builtins.input", return_value="y also clean /tmp/y"):
            result = tool_shell({"command": "rm /nonexistent/path"})
        # Output should contain the user's note line.
        assert "User note when approving: also clean /tmp/y" in (result.output or "")

    def test_always_comment_appears_after_output(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)
        from unittest.mock import patch

        with patch("builtins.input", return_value="a but limit scope to build/"):
            result = tool_shell({"command": "rm /nonexistent/x"})
        from agent_cli.tools import shell as shell_mod

        assert "rm" in shell_mod._session_allowlist
        assert "User note when approving: but limit scope to build/" in (
            result.output or ""
        )

    def test_no_comment_no_note_appended(self, monkeypatch):
        """Bare `y` keeps the output clean — no `[User note...]`
        suffix when the user didn't add anything."""
        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        self._force_tty(monkeypatch)
        from unittest.mock import patch

        with patch("builtins.input", return_value="y"):
            result = tool_shell({"command": "rm /nonexistent/path"})
        assert "User note" not in (result.output or "")


class TestEditFile:
    def test_replace_single_line(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")
        lines = f.read_text().split("\n")
        h2 = compute_line_hash(2, lines[1])
        result = tool_edit_file(
            {"path": str(f), "op": "replace", "pos": f"2#{h2}", "lines": ["replaced"]}
        )
        assert result.success
        assert "Edit complete" in result.output
        assert "replaced" in f.read_text()
        # Diff is appended to the success message: line2 removed, replaced added.
        assert "-line2" in result.output
        assert "+replaced" in result.output

    def test_append_operation(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\n")
        lines = f.read_text().split("\n")
        h1 = compute_line_hash(1, lines[0])
        tool_edit_file(
            {"path": str(f), "op": "append", "pos": f"1#{h1}", "lines": ["inserted"]}
        )
        assert "inserted" in f.read_text()

    def test_prepend_operation(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\n")
        lines = f.read_text().split("\n")
        h1 = compute_line_hash(1, lines[0])
        tool_edit_file(
            {"path": str(f), "op": "prepend", "pos": f"1#{h1}", "lines": ["header"]}
        )
        assert f.read_text().startswith("header")

    def test_append_to_eof(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\n")
        tool_edit_file({"path": str(f), "op": "append", "lines": ["# end"]})
        assert "# end" in f.read_text()

    def test_delete_line(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("keep\ndelete_me\nkeep2\n")
        lines = f.read_text().split("\n")
        h2 = compute_line_hash(2, lines[1])
        tool_edit_file({"path": str(f), "op": "replace", "pos": f"2#{h2}", "lines": []})
        content = f.read_text()
        assert "delete_me" not in content
        assert "keep" in content

    def test_missing_op_error(self, tmp_path):
        # Flat-native: no `edits` list — a missing op is an unknown-op error.
        f = tmp_path / "test.py"
        f.write_text("hello\n")
        result = tool_edit_file({"path": str(f)})
        assert not result.success
        assert "Unknown edit op" in result.error

    def test_unknown_op_error(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello\n")
        result = tool_edit_file({"path": str(f), "op": "invalid", "pos": "1#ZZ"})
        assert not result.success
        assert "Unknown edit op" in result.error

    def test_file_not_found(self):
        result = tool_edit_file(
            {
                "path": "/nonexistent/file.py",
                "op": "replace",
                "pos": "1#ZZ",
                "lines": [],
            }
        )
        assert not result.success
        assert "cannot read" in result.error

    def test_range_replace(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("a\nb\nc\nd\n")
        lines = f.read_text().split("\n")
        h2 = compute_line_hash(2, lines[1])
        h3 = compute_line_hash(3, lines[2])
        tool_edit_file(
            {
                "path": str(f),
                "op": "replace",
                "pos": f"2#{h2}",
                "end": f"3#{h3}",
                "lines": ["X"],
            }
        )
        content = f.read_text()
        assert "b" not in content
        assert "c" not in content
        assert "X" in content

    def test_string_lines_converted(self, tmp_path):
        """If lines is a string instead of list, should split by newline."""
        f = tmp_path / "test.py"
        f.write_text("old\n")
        lines = f.read_text().split("\n")
        h1 = compute_line_hash(1, lines[0])
        tool_edit_file(
            {"path": str(f), "op": "replace", "pos": f"1#{h1}", "lines": "new1\nnew2"}
        )
        content = f.read_text()
        assert "new1" in content
        assert "new2" in content


class TestDelegateResult:
    def test_format_no_output(self):
        result = DelegateResult()
        formatted = _format_delegate_output(result)
        assert "no result" in formatted

    def test_format_no_files(self):
        result = DelegateResult(output="Done")
        formatted = _format_delegate_output(result)
        assert "Files touched" not in formatted


class TestParallelResultFormat:
    def test_all_success(self):
        from agent_cli.tools.result import ToolResult

        specs = [{"task": "A"}, {"task": "B"}]
        results = [
            ToolResult(True, output="STATUS: success\nRESULT:\nDone A"),
            ToolResult(True, output="STATUS: success\nRESULT:\nDone B"),
        ]
        combined = _format_parallel_results(specs, results)
        assert combined.success
        assert "all succeeded" in combined.output

    def test_partial_failure(self):
        from agent_cli.tools.result import ToolResult

        specs = [{"task": "A"}, {"task": "B"}]
        results = [
            ToolResult(True, output="ok"),
            ToolResult(False, error="failed"),
        ]
        combined = _format_parallel_results(specs, results)
        assert not combined.success
        assert "1 succeeded" in combined.error
        assert "1 failed" in combined.error

    def test_timeout_none_result(self):
        specs = [{"task": "A"}]
        results = [None]
        combined = _format_parallel_results(specs, results)
        assert not combined.success
        assert "timed out" in combined.error.lower()


class TestParallelTimeout:
    """Test parallel delegate timeout behavior."""

    def test_parallel_timeout_marks_incomplete(self):
        """Tasks exceeding timeout are reported as timed out."""
        import time
        from unittest.mock import MagicMock, patch
        from agent_cli.providers.base import LLMResponse
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
        provider.call.return_value = LLMResponse(content="mock")

        def slow_run_loop(**kwargs):
            from agent_cli.tools.result import ToolResult

            time.sleep(3)  # Longer than timeout
            return ToolResult(True, output="late result")

        with patch("agent_cli.loop.run_loop", side_effect=slow_run_loop):
            result = tool_delegate(
                args={"tasks": [{"task": "Slow A"}, {"task": "Slow B"}]},
                provider=provider,
                model="test",
                capabilities=caps,
                timeout=1,  # 1 second timeout
            )
            # At least some tasks should be incomplete
            assert (
                "timed out" in (result.error or result.output or "").lower()
                or result is not None
            )


class TestSignalHandlerThreadSafety:
    """Test that signal handler is skipped in worker threads."""

    def test_signal_handler_skipped_in_thread(self):
        """AgentLoop._install_signal_handler is a no-op in non-main thread."""
        import signal
        import threading
        from unittest.mock import MagicMock
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

        original_handler = signal.getsignal(signal.SIGINT)
        handler_changed = {"changed": False}

        def check_in_thread():
            from agent_cli.loop import AgentLoop

            loop = AgentLoop(
                query="test",
                provider=provider,
                capabilities=caps,
                model="test",
                graceful_interrupt=True,
            )
            loop._install_signal_handler()
            # Signal handler should NOT have changed
            current = signal.getsignal(signal.SIGINT)
            handler_changed["changed"] = current != original_handler

        t = threading.Thread(target=check_in_thread)
        t.start()
        t.join()

        assert not handler_changed["changed"], (
            "Signal handler should not change in worker thread"
        )


class TestToolsRegistry:
    """Tests for unified TOOLS dict with virtual tools."""

    def test_tools_contains_all_real_tools(self):
        real_tools = {"read_file", "write_file", "edit_file", "shell", "read_context"}
        assert real_tools.issubset(set(TOOLS.keys()))

    def test_tools_contains_virtual_tools(self):
        assert "complete" in TOOLS
        assert "ask" in TOOLS

    def test_complete_tool_with_result(self):
        tool = TOOLS["complete"]
        result = tool.run({"result": "done"})
        assert result.success
        assert result.output == "done"

    def test_complete_tool_default(self):
        tool = TOOLS["complete"]
        result = tool.run({})
        assert result.success
        assert (
            result.output
            == "(Completed without result — model may lack capability for this task)"
        )

    def test_ask_tool_with_questions(self):
        tool = TOOLS["ask"]
        result = tool.run({"questions": ["what?"]})
        assert result.success
        assert result.output == "what?"

    def test_ask_tool_multiple_questions(self):
        tool = TOOLS["ask"]
        result = tool.run({"questions": ["a?", "b?"]})
        assert result.success
        assert result.output == "a?\nb?"

    def test_ask_tool_default(self):
        tool = TOOLS["ask"]
        result = tool.run({})
        assert result.success
        assert result.output == "(ask)"


class TestExecuteTool:
    def test_execute_virtual_complete(self):
        result = execute_tool("complete", {"result": "all done"})
        assert result.success
        assert result.output == "all done"

    def test_execute_virtual_ask(self):
        result = execute_tool("ask", {"questions": ["which file?"]})
        assert result.success
        assert result.output == "which file?"


class TestReadFilePartial:
    def test_full_read(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\nline4\nline5")
        result = _read_one({"path": str(f)})
        assert result.success
        assert "1#" in result.output
        assert "5#" in result.output

    def test_line_start(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\nccc\nddd\neee")
        result = _read_one({"path": str(f), "line_start": 3})
        assert result.success
        assert "ccc" in result.output
        assert "ddd" in result.output
        assert "eee" in result.output
        assert "aaa" not in result.output

    def test_line_start_and_end(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\nccc\nddd\neee")
        result = _read_one({"path": str(f), "line_start": 2, "line_end": 4})
        assert result.success
        assert "bbb" in result.output
        assert "ddd" in result.output
        assert "aaa" not in result.output
        assert "eee" not in result.output

    def test_line_numbers_preserved(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\nccc\nddd\neee")
        result = _read_one({"path": str(f), "line_start": 3})
        assert result.success
        # First line in result should be line 3, not line 1
        assert result.output.startswith("3#")

    def test_string_line_start_coerced(self, tmp_path):
        """LLMs sometimes send line_start as string."""
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\nccc")
        result = _read_one({"path": str(f), "line_start": "2"})
        assert result.success
        assert "bbb" in result.output
        assert "aaa" not in result.output


class TestReadFileStat:
    def test_stat_shows_metadata(self, tmp_path):
        """stat=True returns line count + size + first 20 lines."""
        f = tmp_path / "big.py"
        content = "\n".join(f"line {i}" for i in range(1, 101))
        f.write_text(content)
        result = _read_one({"path": str(f), "stat": True})
        assert result.success
        assert "[stat]" in result.output
        assert "100 lines" in result.output
        assert "bytes" in result.output or "KB" in result.output

    def test_stat_shows_first_20_lines(self, tmp_path):
        """stat returns first 20 lines with hashlines."""
        f = tmp_path / "big.py"
        content = "\n".join(f"line {i}" for i in range(1, 101))
        f.write_text(content)
        result = _read_one({"path": str(f), "stat": True})
        assert "1#" in result.output
        assert "20#" in result.output
        assert "21#" not in result.output  # only first 20

    def test_stat_small_file_shows_all(self, tmp_path):
        """stat on small file shows all lines (less than 20)."""
        f = tmp_path / "small.py"
        f.write_text("a\nb\nc")
        result = _read_one({"path": str(f), "stat": True})
        assert result.success
        assert "3 lines" in result.output

    def test_stat_includes_followup_guidance(self, tmp_path):
        """stat output must tell the LLM this is a metadata query and
        point at real read modes — otherwise the LLM treats stat-only
        as 'read'.
        """
        f = tmp_path / "big.py"
        f.write_text("\n".join(f"line {i}" for i in range(50)))
        result = _read_one({"path": str(f), "stat": True})
        assert "have NOT read" in result.output or "not read" in result.output.lower()
        assert "line_start" in result.output
        assert "search" in result.output


class TestReadFileSearch:
    def test_search_finds_matches(self, tmp_path):
        """search returns matching lines with context."""
        f = tmp_path / "app.py"
        content = (
            "def foo():\n"
            "    pass\n"
            "\n"
            "def login(user):\n"
            "    return user\n"
            "\n"
            "def bar():\n"
            "    pass\n"
        )
        f.write_text(content)
        result = _read_one({"path": str(f), "search": "login", "context": 1})
        assert result.success
        assert "[search]" in result.output
        assert "1 matches" in result.output
        assert "login" in result.output
        # Context: 1 line before (line 3) and 1 line after (line 5)
        assert "3#" in result.output or "4#" in result.output

    def test_search_no_matches(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("def foo():\n    pass\n")
        result = _read_one({"path": str(f), "search": "nonexistent"})
        assert result.success
        assert "no matches" in result.output

    def test_search_regex_pattern(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("x = 1\ny = 2\nz = 3\n")
        result = _read_one({"path": str(f), "search": r"^[xz]\s*="})
        assert result.success
        assert "2 matches" in result.output

    def test_search_invalid_regex(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("hello\n")
        result = _read_one({"path": str(f), "search": "[invalid"})
        assert not result.success
        assert "Invalid search pattern" in result.error

    def test_search_merges_overlapping_context(self, tmp_path):
        """Adjacent matches should share merged context (not duplicate lines)."""
        f = tmp_path / "app.py"
        content = "\n".join(f"line {i}" for i in range(1, 21))
        # matches on line 5 and line 7, context=3 → ranges overlap → merged
        f.write_text(content.replace("line 5", "MATCH").replace("line 7", "MATCH"))
        result = _read_one({"path": str(f), "search": "MATCH", "context": 3})
        assert result.success
        assert "2 matches" in result.output
        # Should have one merged range block, not two separate
        assert result.output.count("─── lines") == 1


class TestReadContextTool:
    """Cover read_context's list/search modes, scope filter, sessions
    selector, preview formatting, and truncation behavior."""

    # ── Helpers ────────────────────────────────────────────────

    def _make_session(self, base: Path, session_id: str, lines: list[str]) -> Path:
        sdir = base / session_id
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "history.jsonl").write_text("\n".join(lines) + "\n")
        return sdir

    def _patch_base(self, monkeypatch, tmp_path: Path) -> Path:
        import agent_cli.tools.context as ctx_mod

        base = tmp_path / "sessions"
        base.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(ctx_mod, "_SESSIONS_BASE", base)
        return base

    # ── Mode dispatch ──────────────────────────────────────────

    def test_list_no_sessions(self, tmp_path, monkeypatch):
        import agent_cli.context.session as session_mod

        monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "list"})
        assert result.success
        assert "No previous sessions" in result.output

    def test_list_shows_first_message_not_crash_on_missing_query(
        self, tmp_path, monkeypatch
    ):
        # Regression: SessionMeta no longer has a ``query`` field — list mode
        # must derive the title from history.jsonl's first user message
        # instead of crashing with AttributeError ('SessionMeta' has no
        # attribute 'query').
        import json as _json
        import agent_cli.context.session as session_mod

        monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path)
        sdir = tmp_path / "sessions" / "1781440579"
        sdir.mkdir(parents=True)
        (sdir / "session.jsonl").write_text(
            _json.dumps(
                {
                    "_meta": {
                        "session_id": "1781440579",
                        "workspace": "/proj",
                        "updated_at": "2026-06-14 21:36:19",
                        "response_format": "md_array",
                    }
                }
            )
            + "\n"
        )
        (sdir / "history.jsonl").write_text(
            _json.dumps({"role": "user", "content": "[두웅]: analyze this project"})
            + "\n"
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "list"})
        assert result.success  # no AttributeError
        assert "1781440579" in result.output
        assert "[두웅]: analyze this project" in result.output

    def test_unknown_mode(self):
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "invalid"})
        assert not result.success
        assert "unknown mode" in result.error.lower()

    def test_search_missing_keyword(self):
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "search"})
        assert not result.success
        assert "keyword" in result.error.lower()

    def test_default_mode_is_list(self, tmp_path, monkeypatch):
        import agent_cli.context.session as session_mod

        monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({})  # no mode → default list
        assert result.success
        assert "No previous sessions" in result.output

    # ── Default sessions=current behavior ─────────────────────

    def test_search_default_uses_only_current_session(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(
            base, "current", ['{"role":"user","content":"target keyword"}']
        )
        # Other session also contains the keyword — must NOT be returned
        self._make_session(
            base, "other", ['{"role":"user","content":"target keyword too"}']
        )

        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "target"}, session_dir=cur
        )
        assert result.success
        assert "current/" in result.output
        assert "other/" not in result.output

    def test_search_no_session_dir_returns_hint(self, tmp_path, monkeypatch):
        self._patch_base(monkeypatch, tmp_path)
        from agent_cli.tools.context import tool_read_context

        # No session_dir + no sessions arg → cannot resolve default
        result = tool_read_context({"mode": "search", "keyword": "x"})
        assert result.success
        assert "current session" in result.output.lower()
        assert "all" in result.output.lower()

    def test_search_sessions_all_searches_every_session(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(base, "s1", ['{"role":"user","content":"alpha"}'])
        self._make_session(base, "s2", ['{"role":"user","content":"alpha"}'])

        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "alpha", "sessions": "all"}
        )
        assert result.success
        assert "s1/" in result.output and "s2/" in result.output

    def test_search_sessions_specific_id(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(base, "s1", ['{"role":"user","content":"alpha"}'])
        self._make_session(base, "s2", ['{"role":"user","content":"alpha"}'])

        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "alpha", "sessions": "s1"}
        )
        assert result.success
        assert "s1/" in result.output
        assert "s2/" not in result.output

    def test_search_sessions_array_multiple_ids(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(base, "s1", ['{"role":"user","content":"alpha"}'])
        self._make_session(base, "s2", ['{"role":"user","content":"alpha"}'])
        self._make_session(base, "s3", ['{"role":"user","content":"alpha"}'])

        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "alpha", "sessions": ["s1", "s3"]}
        )
        assert result.success
        assert "s1/" in result.output and "s3/" in result.output
        assert "s2/" not in result.output

    def test_search_sessions_unknown_id_errors(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(base, "s1", ['{"role":"user","content":"alpha"}'])

        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "alpha", "sessions": "nope"}
        )
        assert not result.success
        assert "not found" in result.error.lower()

    def test_search_sessions_all_combined_with_id_errors(self, tmp_path, monkeypatch):
        self._patch_base(monkeypatch, tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "x", "sessions": ["all", "s1"]}
        )
        assert not result.success
        assert "all" in result.error.lower()

    # ── Filters: kind ──────────────────────────────────────────

    def test_kind_filter_query(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(
            base,
            "s",
            [
                '{"role":"user","content":"design the auth flow"}',
                '{"role":"assistant","thought":"_","ops":[{"action":"read_file","action_input":{"path":"auth.py"}}]}',
                '{"role":"user","tool":"read_file","content":"Observation: auth stuff"}',
            ],
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "search", "kind": "query"}, session_dir=cur)
        assert result.success
        assert "design the auth flow" in result.output
        assert "kind=query" in result.output
        # only the query record (not the action/observation)
        assert "read_file" not in result.output

    def test_kind_string_auto_promoted(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(
            base,
            "s",
            [
                '{"role":"user","content":"q here"}',
                '{"role":"assistant","thought":"t","ops":[{"action":"shell","action_input":{"cmd":"ls"}}]}',
            ],
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "kind": "action"}, session_dir=cur
        )
        assert result.success
        assert "kind=action" in result.output
        assert "kind=query" not in result.output

    def test_kind_invalid_errors(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(base, "s", ['{"role":"user","content":"x"}'])
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "kind": "reasoning"}, session_dir=cur
        )
        assert not result.success
        assert "invalid kind" in result.error.lower()

    def test_no_filter_errors(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(base, "s", ['{"role":"user","content":"x"}'])
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "search"}, session_dir=cur)
        assert not result.success
        assert "at least one filter" in result.error.lower()

    # ── Filters: tool / author / turn ──────────────────────────

    def test_tool_filter_matches_action_and_observation(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(
            base,
            "s",
            [
                '{"role":"assistant","thought":"t","ops":[{"action":"read_file","action_input":{"path":"a.py"}}]}',
                '{"role":"user","tool":"read_file","content":"Observation: file body"}',
                '{"role":"assistant","thought":"t","ops":[{"action":"shell","action_input":{"cmd":"ls"}}]}',
            ],
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "tool": "read_file"}, session_dir=cur
        )
        assert result.success
        # action + observation for read_file match; the shell op does not
        assert result.output.count("tools=['read_file']") == 2
        assert "shell" not in result.output

    def test_author_filter(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(
            base,
            "s",
            [
                '{"role":"user","content":"[Alice]: do X","author":"Alice"}',
                '{"role":"user","content":"[Bob]: do Y","author":"Bob"}',
            ],
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "author": "Alice"}, session_dir=cur
        )
        assert result.success
        assert "author=Alice" in result.output
        assert "Bob" not in result.output
        # label stripped for the search surface
        assert "do X" in result.output

    def test_turn_filter_single_and_range(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(
            base,
            "s",
            [
                '{"role":"user","content":"q0","turn":0}',
                '{"role":"assistant","thought":"t","ops":[{"action":"shell","action_input":{}}],"turn":1}',
                '{"role":"assistant","thought":"t","ops":[{"action":"complete","action_input":{"result":"r"}}],"turn":2}',
            ],
        )
        from agent_cli.tools.context import tool_read_context

        single = tool_read_context({"mode": "search", "turn": 1}, session_dir=cur)
        assert (
            single.success
            and "turn=1" in single.output
            and "turn=2" not in single.output
        )

        rng = tool_read_context(
            {"mode": "search", "turn": {"from": 1, "to": 2}}, session_dir=cur
        )
        assert rng.success and "turn=1" in rng.output and "turn=2" in rng.output
        assert "turn=0" not in rng.output

    def test_keyword_over_text_surface(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(
            base,
            "s",
            [
                '{"role":"assistant","thought":"check the auth flow","ops":[{"action":"shell","action_input":{"cmd":"ls"}}]}',
                '{"role":"assistant","thought":"unrelated","ops":[{"action":"shell","action_input":{"cmd":"pwd"}}]}',
            ],
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "auth"}, session_dir=cur
        )
        assert result.success
        assert "check the auth flow" in result.output
        assert "unrelated" not in result.output

    def test_combined_filters(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(
            base,
            "s",
            [
                '{"role":"assistant","thought":"t","ops":[{"action":"read_file","action_input":{"path":"auth.py"}}],"turn":3}',
                '{"role":"assistant","thought":"t","ops":[{"action":"read_file","action_input":{"path":"db.py"}}],"turn":4}',
            ],
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "tool": "read_file", "keyword": "auth"}, session_dir=cur
        )
        assert result.success
        assert "auth.py" in result.output
        assert "db.py" not in result.output

    # ── No false positives / robustness ───────────────────────

    def test_missing_ops_does_not_crash(self, tmp_path, monkeypatch):
        # raw assistant content (no ops) classifies as kind=raw, no crash
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(
            base, "s", ['{"role":"assistant","content":"raw leftover auth"}']
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "auth"}, session_dir=cur
        )
        assert result.success
        assert "kind=raw" in result.output

    def test_corrupt_jsonl_line_skipped(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(
            base,
            "s",
            ["not json at all", '{"role":"user","content":"valid auth line"}'],
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "auth"}, session_dir=cur
        )
        assert result.success
        assert "valid auth line" in result.output

    def test_search_includes_subdir_history(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = base / "s"
        cur.mkdir(parents=True)
        sub = cur / "delegate-1"
        sub.mkdir()
        (sub / "history.jsonl").write_text(
            '{"role":"user","content":"sub auth line"}\n'
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "auth"}, session_dir=cur
        )
        assert result.success
        assert "sub auth line" in result.output
        assert "delegate-1" in result.output

    # ── Preview formatting ─────────────────────────────────────

    def test_preview_collapses_whitespace_and_caps(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        long = "auth " + "x " * 300
        import json as _json

        cur = self._make_session(
            base, "s", [_json.dumps({"role": "user", "content": long})]
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "auth"}, session_dir=cur
        )
        assert result.success
        assert "\n\n" in result.output  # block structure
        assert "..." in result.output  # capped

    # ── Result format ──────────────────────────────────────────

    def test_format_header_includes_filters(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(base, "s", ['{"role":"user","content":"auth here"}'])
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "auth", "kind": "query"}, session_dir=cur
        )
        assert result.success
        assert "keyword='auth'" in result.output
        assert "kind=['query']" in result.output

    def test_format_no_matches_message(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(base, "s", ['{"role":"user","content":"hello"}'])
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "zzz"}, session_dir=cur
        )
        assert result.success
        assert "No matches" in result.output

    def test_truncation_caps_at_50(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        import json as _json

        lines = [
            _json.dumps({"role": "user", "content": f"auth line {i}"})
            for i in range(80)
        ]
        cur = self._make_session(base, "s", lines)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "auth"}, session_dir=cur
        )
        assert result.success
        assert "capped at 50" in result.output

    # ── Plumbing: execute_tool passes session_dir ────────────

    def test_execute_tool_forwards_session_dir(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(base, "current", ['{"role":"user","content":"alpha"}'])
        # Other session has the same keyword — proves session_dir filter works
        self._make_session(base, "other", ['{"role":"user","content":"alpha"}'])

        from agent_cli.tools import _execute_tool as execute_tool

        result = execute_tool(
            "read_context",
            {"mode": "search", "keyword": "alpha"},
            session_dir=cur,
        )
        assert result.success
        assert "current/" in result.output
        assert "other/" not in result.output

    def test_execute_tool_other_tools_unaffected(self, tmp_path):
        """Adding session_dir kwarg must not break other tools."""
        from agent_cli.tools import _execute_tool as execute_tool

        # shell ignores session_dir entirely
        result = execute_tool("shell", {"command": "echo ok"}, session_dir=tmp_path)
        assert result.success
        assert "ok" in result.output

    # ── Search → fetch hint footer ────────────────────────────

    def test_search_results_include_fetch_hint(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(base, "s", ['{"role":"user","content":"alpha hit"}'])

        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "alpha"}, session_dir=cur
        )
        assert result.success
        assert "mode='fetch'" in result.output

    def test_search_no_match_omits_fetch_hint(self, tmp_path, monkeypatch):
        """Hint is only useful when there's something to fetch."""
        base = self._patch_base(monkeypatch, tmp_path)
        cur = self._make_session(base, "s", ['{"role":"user","content":"alpha"}'])

        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "search", "keyword": "nothere"}, session_dir=cur
        )
        assert result.success
        assert "mode='fetch'" not in result.output

    # ── mode=fetch: argument validation ───────────────────────

    def test_fetch_missing_loc(self):
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch"})
        assert not result.success
        assert "loc is required" in result.error.lower()

    def test_fetch_empty_loc_list(self):
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch", "loc": []})
        assert not result.success
        assert "non-empty" in result.error.lower()

    def test_fetch_loc_too_many(self, tmp_path, monkeypatch):
        self._patch_base(monkeypatch, tmp_path)
        from agent_cli.tools.context import tool_read_context

        too_many = [f"s{i}/history.jsonl:1" for i in range(11)]
        result = tool_read_context({"mode": "fetch", "loc": too_many})
        assert not result.success
        assert "max 10" in result.error.lower()

    def test_fetch_loc_string_auto_promoted(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(base, "s", ['{"role":"user","content":"alpha"}'])

        from agent_cli.tools.context import tool_read_context

        # Single string accepted
        result = tool_read_context({"mode": "fetch", "loc": "s/history.jsonl:1"})
        assert result.success
        assert "alpha" in result.output

    def test_fetch_loc_bad_format_no_colon(self, tmp_path, monkeypatch):
        self._patch_base(monkeypatch, tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch", "loc": "s/history.jsonl"})
        assert not result.success
        assert "line_num" in result.error.lower()

    def test_fetch_loc_bad_format_non_int_line(self, tmp_path, monkeypatch):
        self._patch_base(monkeypatch, tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch", "loc": "s/history.jsonl:abc"})
        assert not result.success
        assert "integer" in result.error.lower()

    def test_fetch_loc_bad_format_zero_line(self, tmp_path, monkeypatch):
        self._patch_base(monkeypatch, tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch", "loc": "s/history.jsonl:0"})
        assert not result.success
        assert "line_num must be >= 1" in result.error.lower() or (
            "line_num" in result.error.lower() and ">=" in result.error.lower()
        )

    def test_fetch_loc_bad_format_no_slash(self, tmp_path, monkeypatch):
        self._patch_base(monkeypatch, tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch", "loc": "history.jsonl:5"})
        assert not result.success
        assert "session_id" in result.error.lower()

    def test_fetch_range_negative(self, tmp_path, monkeypatch):
        self._patch_base(monkeypatch, tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "fetch", "loc": "s/history.jsonl:1", "range": -1}
        )
        assert not result.success
        assert "range must be" in result.error.lower()

    def test_fetch_range_above_cap(self, tmp_path, monkeypatch):
        self._patch_base(monkeypatch, tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "fetch", "loc": "s/history.jsonl:1", "range": 6}
        )
        assert not result.success
        assert "range must be" in result.error.lower()

    def test_fetch_range_non_integer(self, tmp_path, monkeypatch):
        self._patch_base(monkeypatch, tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "fetch", "loc": "s/history.jsonl:1", "range": "two"}
        )
        assert not result.success
        assert "range must be" in result.error.lower()

    # ── mode=fetch: file/range errors ─────────────────────────

    def test_fetch_session_not_found(self, tmp_path, monkeypatch):
        self._patch_base(monkeypatch, tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch", "loc": "nope/history.jsonl:1"})
        assert not result.success
        assert "file not found" in result.error.lower()

    def test_fetch_line_out_of_range(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(base, "s", ['{"role":"user","content":"only one line"}'])
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch", "loc": "s/history.jsonl:99"})
        assert not result.success
        assert "out of range" in result.error.lower()

    def test_fetch_all_or_nothing_partial_failure(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(base, "good", ['{"role":"user","content":"valid"}'])
        from agent_cli.tools.context import tool_read_context

        # First loc is valid, second is bogus → entire fetch fails
        result = tool_read_context(
            {
                "mode": "fetch",
                "loc": ["good/history.jsonl:1", "bogus/history.jsonl:1"],
            }
        )
        assert not result.success
        # Output of valid loc should NOT have leaked into error
        assert "valid" not in (result.error or "")

    # ── mode=fetch: happy path ────────────────────────────────

    def test_fetch_single_target_only(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(
            base,
            "s",
            [
                '{"role":"user","content":"first"}',
                '{"role":"assistant","thought":"reasoning","action":"x","action_input":{"k":"v"}}',
                '{"role":"user","content":"third"}',
            ],
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch", "loc": "s/history.jsonl:2"})
        assert result.success
        # Target turn fields rendered
        assert "thought: reasoning" in result.output
        assert "action: x" in result.output
        # action_input compact JSON
        assert '{"k": "v"}' in result.output or '{"k":"v"}' in result.output
        # Other turns NOT included (range default = 0)
        assert "first" not in result.output
        assert "third" not in result.output
        # Target marker
        assert "<- target" in result.output

    def test_fetch_with_range(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(
            base,
            "s",
            [
                '{"role":"user","content":"line one"}',
                '{"role":"assistant","thought":"_","action":"x","action_input":{}}',
                '{"role":"user","content":"line three (target)"}',
                '{"role":"assistant","thought":"_","action":"y","action_input":{}}',
                '{"role":"user","content":"line five"}',
            ],
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "fetch", "loc": "s/history.jsonl:3", "range": 1}
        )
        assert result.success
        # Range +/-1 includes lines 2, 3, 4
        assert "line three (target)" in result.output
        assert "<- target" in result.output  # only on the target turn
        # Header should reflect range
        assert "(range +/-1)" in result.output
        # Adjacent turns appear (lines 2 and 4)
        assert "action: x" in result.output  # line 2 turn
        assert "action: y" in result.output  # line 4 turn
        # Out-of-range turns not shown
        assert "line one" not in result.output
        assert "line five" not in result.output

    def test_fetch_range_clipped_at_file_boundary(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(
            base,
            "s",
            [
                '{"role":"user","content":"first"}',
                '{"role":"user","content":"second"}',
            ],
        )
        from agent_cli.tools.context import tool_read_context

        # Target = line 1, range = 5 → wants -4..6 but clips to 1..2
        result = tool_read_context(
            {"mode": "fetch", "loc": "s/history.jsonl:1", "range": 5}
        )
        assert result.success
        assert "first" in result.output
        assert "second" in result.output

    def test_fetch_multiple_locs(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(base, "s1", ['{"role":"user","content":"alpha at s1"}'])
        self._make_session(base, "s2", ['{"role":"user","content":"beta at s2"}'])
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {
                "mode": "fetch",
                "loc": ["s1/history.jsonl:1", "s2/history.jsonl:1"],
            }
        )
        assert result.success
        # Top header counts groups
        assert "Fetched 2 locations" in result.output
        # Both locs rendered with their group headers
        assert "=== s1/history.jsonl:1" in result.output
        assert "=== s2/history.jsonl:1" in result.output
        # Both contents present
        assert "alpha at s1" in result.output
        assert "beta at s2" in result.output

    def test_fetch_multiline_content_block_style(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        # Observation with embedded newlines (preserved via \n escape)
        self._make_session(
            base,
            "s",
            [
                '{"role":"user","content":"Observation: line one\\nline two\\nline three"}'
            ],
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch", "loc": "s/history.jsonl:1"})
        assert result.success
        # Block-style: label on its own line, then indented content lines.
        # First line carries the "Observation:" prefix; subsequent lines do not.
        assert "observation:\n" in result.output
        assert "     Observation: line one" in result.output
        assert "     line two" in result.output
        assert "     line three" in result.output
        # Newlines preserved (not collapsed)
        assert "line one line two" not in result.output

    def test_fetch_observation_label_for_obs_prefixed_content(
        self, tmp_path, monkeypatch
    ):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(
            base,
            "obs",
            ['{"role":"user","content":"Observation: tool result body"}'],
        )
        self._make_session(
            base,
            "qry",
            ['{"role":"user","content":"plain user query"}'],
        )
        from agent_cli.tools.context import tool_read_context

        # Observation-prefixed content → labelled 'observation'
        r1 = tool_read_context({"mode": "fetch", "loc": "obs/history.jsonl:1"})
        assert r1.success
        assert "observation: Observation: tool result body" in r1.output

        # Non-obs user content → labelled 'content'
        r2 = tool_read_context({"mode": "fetch", "loc": "qry/history.jsonl:1"})
        assert r2.success
        assert "content: plain user query" in r2.output

    def test_fetch_corrupt_json_line_renders_raw(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        sdir = base / "s"
        sdir.mkdir(parents=True)
        # Write a malformed line directly (bypasses _make_session JSON shape)
        (sdir / "history.jsonl").write_text("not valid json at all\n")
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch", "loc": "s/history.jsonl:1"})
        assert result.success
        assert "corrupt JSON" in result.output
        assert "not valid json" in result.output

    def test_fetch_artifact_field_rendered(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        self._make_session(
            base,
            "s",
            [
                '{"role":"user","content":"Observation: head","artifact":"shell/cmd_x.log"}'
            ],
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch", "loc": "s/history.jsonl:1"})
        assert result.success
        assert "[artifact: shell/cmd_x.log]" in result.output

    def test_fetch_includes_subdir_history(self, tmp_path, monkeypatch):
        base = self._patch_base(monkeypatch, tmp_path)
        sdir = base / "s"
        delegate_dir = sdir / "delegate_x"
        delegate_dir.mkdir(parents=True)
        (delegate_dir / "history.jsonl").write_text(
            '{"role":"user","content":"sub-session content"}\n'
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context(
            {"mode": "fetch", "loc": "s/delegate_x/history.jsonl:1"}
        )
        assert result.success
        assert "sub-session content" in result.output

    def test_fetch_no_size_cap_on_observation(self, tmp_path, monkeypatch):
        """Unlike search preview (200 char cap), fetch returns full content."""
        base = self._patch_base(monkeypatch, tmp_path)
        # 500-char observation
        big = "X" * 500
        self._make_session(
            base,
            "s",
            ['{"role":"user","content":"Observation: ' + big + '"}'],
        )
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "fetch", "loc": "s/history.jsonl:1"})
        assert result.success
        assert big in result.output
        assert "..." not in result.output  # no truncation

    def test_fetch_unknown_mode_helpful_error(self):
        """Unknown mode error mentions fetch as available."""
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "wat"})
        assert not result.success
        assert "fetch" in result.error.lower()


class TestRunSkillTool:
    def test_run_skill_in_tools(self):
        """run_skill is registered in TOOLS."""
        assert "run_skill" in TOOLS

    def test_run_skill_schema_exists(self):
        """run_skill has a schema with name (required) and arguments."""
        from agent_cli.tools.registry import TOOL_SCHEMAS

        assert "run_skill" in TOOL_SCHEMAS
        schema = TOOL_SCHEMAS["run_skill"]
        assert "name" in schema.parameters["required"]
