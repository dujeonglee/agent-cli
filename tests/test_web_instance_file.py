"""Per-session web instance file — `.agent-cli/sessions/<id>/web.json`.

``agent-cli web`` writes it on start and removes it on exit so an external
orchestrator (the board) can answer "is this session's web up, and where?" from
one file. Pure read/write/remove helpers — no live server needed to test.
"""

from __future__ import annotations

import json
import os

from agent_cli.web.instance_file import (
    instance_file_path,
    read_instance_file,
    remove_instance_file,
    write_instance_file,
)


class TestInstanceFile:
    def test_write_then_read_round_trip(self, tmp_path):
        write_instance_file(
            tmp_path, session_id="1782", host="127.0.0.1", port=50001, token="tok"
        )
        info = read_instance_file(tmp_path)
        assert info["session_id"] == "1782"
        assert info["host"] == "127.0.0.1"
        assert info["port"] == 50001
        assert info["token"] == "tok"

    def test_pid_defaults_to_current_process(self, tmp_path):
        write_instance_file(
            tmp_path, session_id="1782", host="127.0.0.1", port=50001, token="tok"
        )
        assert read_instance_file(tmp_path)["pid"] == os.getpid()

    def test_pid_explicit(self, tmp_path):
        write_instance_file(
            tmp_path, session_id="x", host="h", port=1, token="t", pid=4242
        )
        assert read_instance_file(tmp_path)["pid"] == 4242

    def test_write_creates_missing_dir(self, tmp_path):
        nested = tmp_path / "a" / "b"
        write_instance_file(nested, session_id="x", host="h", port=1, token="t")
        assert (nested / "web.json").exists()

    def test_read_missing_returns_none(self, tmp_path):
        assert read_instance_file(tmp_path) is None

    def test_read_corrupt_returns_none(self, tmp_path):
        instance_file_path(tmp_path).write_text("{not json", encoding="utf-8")
        assert read_instance_file(tmp_path) is None

    def test_remove_deletes_file(self, tmp_path):
        write_instance_file(tmp_path, session_id="x", host="h", port=1, token="t")
        assert instance_file_path(tmp_path).exists()
        remove_instance_file(tmp_path)
        assert not instance_file_path(tmp_path).exists()

    def test_remove_is_idempotent_when_absent(self, tmp_path):
        # no exception when there's nothing to remove
        remove_instance_file(tmp_path)
        remove_instance_file(tmp_path)

    def test_written_json_is_plain_object(self, tmp_path):
        write_instance_file(tmp_path, session_id="x", host="h", port=1, token="t")
        raw = json.loads(instance_file_path(tmp_path).read_text(encoding="utf-8"))
        assert set(raw) == {"session_id", "host", "port", "token", "pid"}
