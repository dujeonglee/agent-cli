"""Conversation export rendering (integrations/export + integrations/jira).

Both renderers are pure functions of the entry list, and the Jira client takes
``base_url`` as an argument, so everything tests without a browser or a live
(paid) Jira — the HTTP POST is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_cli.integrations import jira as jira_mod
from agent_cli.integrations.export import entries_to_adf, entries_to_html

_ENTRIES = [
    {"kind": "user", "label": "User", "body": "refactor auth.py", "mono": False},
    {"kind": "observation", "label": "read_file", "body": "1#AB:line", "mono": True},
]


# ── HTML ─────────────────────────────────────────────────────────────────────


class TestEntriesToHtml:
    def test_standalone_document(self):
        out = entries_to_html(_ENTRIES, title="My session")
        assert out.startswith("<!doctype html>")
        assert "</html>" in out.strip()[-10:]
        # self-contained: styles inlined, no external asset refs
        assert "<style>" in out
        assert 'href="' not in out and 'src="' not in out
        assert "My session" in out

    def test_entries_rendered_with_labels(self):
        out = entries_to_html(_ENTRIES)
        assert "User" in out and "read_file" in out
        assert "refactor auth.py" in out
        # monospace body → <pre>; prose body → <p>
        assert '<pre class="exp-body">1#AB:line</pre>' in out
        assert "exp-user" in out and "exp-observation" in out

    def test_html_escaped(self):
        out = entries_to_html(
            [{"kind": "user", "label": "User", "body": "<script>x</script>"}]
        )
        assert "<script>x" not in out
        assert "&lt;script&gt;" in out

    def test_empty_entries(self):
        out = entries_to_html([])
        assert out.startswith("<!doctype html>")
        assert "0 entries" in out


# ── ADF ──────────────────────────────────────────────────────────────────────


class TestEntriesToAdf:
    def test_doc_shape(self):
        doc = entries_to_adf(_ENTRIES)
        assert doc["type"] == "doc"
        assert doc["version"] == 1
        assert isinstance(doc["content"], list) and doc["content"]

    def test_label_is_strong_paragraph_body_follows(self):
        doc = entries_to_adf(
            [{"kind": "user", "label": "User", "body": "hi", "mono": False}]
        )
        label, body = doc["content"]
        assert label["type"] == "paragraph"
        assert label["content"][0]["text"] == "User"
        assert label["content"][0]["marks"] == [{"type": "strong"}]
        assert body["type"] == "paragraph"
        assert body["content"][0]["text"] == "hi"

    def test_mono_body_is_codeblock(self):
        doc = entries_to_adf(
            [{"kind": "observation", "label": "obs", "body": "x", "mono": True}]
        )
        assert doc["content"][1]["type"] == "codeBlock"

    def test_empty_body_skipped_no_empty_text_node(self):
        # ADF rejects empty text nodes — an entry with no body must emit only
        # the label paragraph (no empty codeBlock/paragraph).
        doc = entries_to_adf([{"kind": "user", "label": "User", "body": ""}])
        assert len(doc["content"]) == 1
        assert doc["content"][0]["content"][0]["text"] == "User"

    def test_empty_entries_yields_placeholder_doc(self):
        doc = entries_to_adf([])
        assert doc["type"] == "doc"
        assert doc["content"][0]["content"][0]["text"] == "(no content)"


# ── Jira instance resolution ─────────────────────────────────────────────────

_CFG = {
    "jira": {
        "instances": {
            "work": {
                "base_url": "https://work.atlassian.net",
                "email": "me@co.com",
                "api_token": "tok-w",
            },
            "oss": {
                "base_url": "https://oss.atlassian.net/",
                "email": "me@x.com",
                "api_token": "tok-o",
            },
        },
        "default": "work",
    }
}


class TestJiraResolution:
    def test_list_targets_excludes_tokens(self):
        targets = jira_mod.list_targets(_CFG)
        names = {t["name"] for t in targets}
        assert names == {"work", "oss"}
        blob = str(targets)
        assert "tok-w" not in blob and "tok-o" not in blob
        assert any(t["default"] for t in targets if t["name"] == "work")

    def test_list_targets_empty_when_unconfigured(self):
        assert jira_mod.list_targets({}) == []

    def test_resolve_default(self):
        inst = jira_mod.resolve_instance(_CFG)
        assert inst["name"] == "work"
        assert inst["base_url"] == "https://work.atlassian.net"
        assert inst["api_token"] == "tok-w"

    def test_resolve_named_strips_trailing_slash(self):
        inst = jira_mod.resolve_instance(_CFG, "oss")
        assert inst["base_url"] == "https://oss.atlassian.net"  # no trailing /

    def test_resolve_unknown_raises(self):
        with pytest.raises(jira_mod.JiraError, match="Unknown Jira instance"):
            jira_mod.resolve_instance(_CFG, "nope")

    def test_resolve_no_config_raises(self):
        with pytest.raises(jira_mod.JiraError, match="No Jira instances"):
            jira_mod.resolve_instance({})

    def test_resolve_missing_fields_raises(self):
        cfg = {"jira": {"instances": {"x": {"base_url": "https://x"}}}, "default": "x"}
        with pytest.raises(jira_mod.JiraError, match="missing"):
            jira_mod.resolve_instance(cfg, "x")


# ── Jira POST (mocked) ───────────────────────────────────────────────────────


class TestPostComment:
    def test_posts_adf_to_correct_url_with_auth(self):
        adf = {"type": "doc", "version": 1, "content": []}
        with patch("agent_cli.integrations.jira.requests.post") as post:
            post.return_value = MagicMock(status_code=201, text="{}")
            url = jira_mod.post_comment(
                "https://work.atlassian.net", "me@co.com", "tok", "PROJ-7", adf
            )
        assert url == "https://work.atlassian.net/browse/PROJ-7"
        call = post.call_args
        assert (
            call.args[0] == "https://work.atlassian.net/rest/api/3/issue/PROJ-7/comment"
        )
        assert call.kwargs["auth"] == ("me@co.com", "tok")
        assert call.kwargs["json"] == {"body": adf}

    def test_blank_issue_key_raises(self):
        with pytest.raises(jira_mod.JiraError, match="Issue key is required"):
            jira_mod.post_comment("https://x", "e", "t", "  ", {})

    def test_jira_error_status_raises(self):
        with patch("agent_cli.integrations.jira.requests.post") as post:
            post.return_value = MagicMock(status_code=403, text="forbidden")
            with pytest.raises(jira_mod.JiraError, match="rejected the comment"):
                jira_mod.post_comment("https://x", "e", "t", "P-1", {})

    def test_transport_error_raises(self):
        import requests

        with patch("agent_cli.integrations.jira.requests.post") as post:
            post.side_effect = requests.ConnectionError("down")
            with pytest.raises(jira_mod.JiraError, match="Could not reach Jira"):
                jira_mod.post_comment("https://x", "e", "t", "P-1", {})
