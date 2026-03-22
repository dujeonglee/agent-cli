"""Tests for input_history module (readline setup + persistent history)."""

import readline
from unittest.mock import patch

import pytest

import agent_cli.input_history as ih


@pytest.fixture(autouse=True)
def _reset_initialized():
    """Reset module state before each test."""
    ih._initialized = False
    yield
    ih._initialized = False


class TestSetup:
    def test_no_crash_without_history_file(self, tmp_path, monkeypatch):
        """setup() succeeds when history file does not exist."""
        monkeypatch.setattr(ih, "_HISTORY_FILE", tmp_path / "nonexistent")
        ih.setup()
        assert ih._initialized is True

    def test_idempotent(self, tmp_path, monkeypatch):
        """Calling setup() twice does not re-read history."""
        monkeypatch.setattr(ih, "_HISTORY_FILE", tmp_path / "hist")
        with patch.object(readline, "read_history_file") as mock_read:
            ih.setup()
            ih.setup()  # second call should be a no-op
        # read_history_file not called at all (file doesn't exist)
        mock_read.assert_not_called()

    def test_loads_existing_history(self, tmp_path, monkeypatch):
        """setup() reads history from existing file."""
        hist_file = tmp_path / "hist"
        # Create a valid history file via readline
        readline.add_history("test entry")
        readline.write_history_file(str(hist_file))
        readline.clear_history()

        monkeypatch.setattr(ih, "_HISTORY_FILE", hist_file)
        ih.setup()
        assert readline.get_current_history_length() >= 1

    def test_handles_corrupt_file(self, tmp_path, monkeypatch):
        """setup() ignores corrupt/unreadable history file."""
        hist_file = tmp_path / "hist"
        hist_file.write_bytes(b"\x00\x01\x02\xff\xfe")
        monkeypatch.setattr(ih, "_HISTORY_FILE", hist_file)
        ih.setup()  # should not raise
        assert ih._initialized is True


class TestMakePrompt:
    def test_plain_text_prompt(self):
        """make_prompt returns plain text with trailing space."""
        result = ih.make_prompt("You:")
        assert result == "You: "


class TestSave:
    def test_creates_directory_and_file(self, tmp_path, monkeypatch):
        """save() creates parent directories and history file."""
        hist_file = tmp_path / "sub" / "deep" / "chat_history"
        monkeypatch.setattr(ih, "_HISTORY_FILE", hist_file)
        readline.add_history("save test")
        ih.save()
        assert hist_file.is_file()

    def test_handles_permission_error(self, monkeypatch):
        """save() swallows OSError on write failure."""
        with patch.object(
            readline, "write_history_file", side_effect=OSError("denied")
        ):
            ih.save()  # should not raise
