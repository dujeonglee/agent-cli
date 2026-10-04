"""Prompt inventory (v10.15.0; ``--html`` v10.16.0) — ``python -m agent_cli.prompts.inventory``.

The inventory is read off the same functions the loop calls. These tests pin
its shape and the few facts a reader relies on; the prompt wording itself is
covered where it is built (test_system_prompt, test_prompt_language).
"""

from __future__ import annotations

import json

import pytest

from agent_cli import __version__, dialects
from agent_cli.prompts.inventory import SCENARIOS, build_inventory, main
from agent_cli.tools.registry import _BUILTIN_ORDER


@pytest.fixture(scope="module")
def inv():
    return build_inventory()


class TestShape:
    def test_every_dialect_and_scenario_is_present(self, inv):
        assert inv["agent_cli_version"] == __version__
        assert sorted(inv["dialects"]) == sorted(dialects.list_names())
        for entry in inv["dialects"].values():
            assert list(entry["scenarios"]) == [sc.key for sc in SCENARIOS]

    def test_it_is_json_serialisable(self, inv):
        assert json.loads(json.dumps(inv)) == inv

    def test_sections_carry_text_and_a_token_estimate(self, inv):
        main_loop = inv["dialects"]["json_fc"]["scenarios"]["main"]
        names = [s["name"] for s in main_loop["sections"]]
        assert names[0] == "Role" and "Available Tools" in names
        assert all(s["text"] and s["tokens"] > 0 for s in main_loop["sections"])
        assert main_loop["system_tokens"] == sum(
            s["tokens"] for s in main_loop["sections"]
        )


class TestScenariosDiffer:
    def test_narrowed_loop_lists_only_its_tools(self, inv):
        narrowed = inv["dialects"]["json_fc"]["scenarios"]["narrowed"]
        assert narrowed["tools"] == ["read_file", "shell", "complete"]
        tools_text = next(
            s["text"] for s in narrowed["sections"] if s["name"] == "Available Tools"
        )
        assert "- edit_file:" not in tools_text
        full = inv["dialects"]["json_fc"]["scenarios"]["main"]
        assert narrowed["system_tokens"] < full["system_tokens"]

    def test_subagent_is_told_run_only(self, inv):
        sub = inv["dialects"]["json_fc"]["scenarios"]["subagent"]
        text = "\n".join(s["text"] for s in sub["sections"])
        assert '"mode": "spawn"' not in text

    def test_server_parsed_dialect_carries_function_schemas(self, inv):
        native = inv["dialects"]["native_fc"]
        assert native["server_parsed"] is True
        sc = native["scenarios"]["narrowed"]
        assert [f["name"] for f in sc["functions"]] == [
            "read_file",
            "shell",
            "complete",
        ]
        assert sc["functions_tokens"] == sum(f["tokens"] for f in sc["functions"])
        # the tools travel as schemas, not as a prompt section
        assert "Available Tools" not in [s["name"] for s in sc["sections"]]
        assert "functions" not in inv["dialects"]["json_fc"]["scenarios"]["main"]


class TestToolsAndRuntime:
    def test_every_builtin_tool_in_prompt_order(self, inv):
        tools = inv["dialects"]["json_fc"]["tools"]
        assert [t["name"] for t in tools] == list(_BUILTIN_ORDER)
        assert all(t["description"] and "parameters" in t for t in tools)

    def test_runtime_texts_are_rendered_per_dialect(self, inv):
        ids = [r["id"] for r in inv["dialects"]["json_fc"]["runtime"]]
        assert len(ids) == len(set(ids))
        assert {"obs_complete_nudge", "format_nudge_no_call", "compaction_notice"} <= (
            set(ids)
        )
        assert all(r["text"] for r in inv["dialects"]["json_fc"]["runtime"])

        def nudge(name):
            return next(
                r["text"]
                for r in inv["dialects"][name]["runtime"]
                if r["id"] == "format_nudge_no_action"
            )

        assert nudge("json_fc") != nudge("xml_fc")


class TestCli:
    def test_writes_the_file(self, tmp_path):
        out = tmp_path / "inventory.json"
        assert main(["-o", str(out)]) == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["agent_cli_version"] == __version__

    def test_stdout_by_default(self, capsys):
        assert main([]) == 0
        assert json.loads(capsys.readouterr().out)["dialects"]


class TestHtml:
    """``--html`` (v10.16.0): the same data as one self-contained page."""

    @staticmethod
    def _embedded(page: str) -> dict:
        import re

        m = re.search(
            r'<script type="application/json" id="inventory-data">(.*?)</script>',
            page,
            re.DOTALL,
        )
        assert m, "data element not found"
        return json.loads(m.group(1))

    def test_page_embeds_exactly_the_inventory(self, inv):
        from agent_cli.prompts.inventory import render_html

        page = render_html(inv)
        assert page.startswith("<!doctype html>")
        assert "__INVENTORY_DATA__" not in page
        assert self._embedded(page) == inv

    def test_page_needs_no_network(self, inv):
        """Opened from disk, or published as is: no external script, style or
        fetch — a prompt URL inside the DATA is fine, a loaded one is not."""
        import re

        from agent_cli.prompts.inventory import render_html

        page = render_html({"dialects": {}, "scenarios": {}})
        assert not re.search(r'(src|href)\s*=\s*["\']https?:', page)
        assert "fetch(" not in page and "@import" not in page

    def test_a_closing_script_tag_in_a_prompt_cannot_end_the_data(self):
        from agent_cli.prompts.inventory import render_html

        hostile = {"text": "</script><script>alert(1)</script>"}
        page = render_html(hostile)
        assert "</script><script>alert(1)" not in page
        assert self._embedded(page) == hostile

    def test_cli_writes_the_page(self, tmp_path):
        out = tmp_path / "inventory.html"
        assert main(["--html", str(out)]) == 0
        data = self._embedded(out.read_text(encoding="utf-8"))
        assert data["agent_cli_version"] == __version__

    def test_template_ships_in_the_wheel(self):
        """Not a .py file — it needs a package-data entry or a pip install
        raises FileNotFoundError on --html."""
        from pathlib import Path

        import agent_cli.prompts.inventory as mod

        assert Path(mod.__file__).with_name("inventory.html").is_file()
        pyproject = Path(mod.__file__).parents[2] / "pyproject.toml"
        assert '"prompts/inventory.html"' in pyproject.read_text(encoding="utf-8")
