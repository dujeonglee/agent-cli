"""Conversation export rendering (integrations/export + integrations/jira).

Both renderers are pure functions of the entry list, and the Jira client takes
``base_url`` as an argument, so everything tests without a browser or a live
(paid) Jira — the HTTP POST is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_cli.integrations import jira as jira_mod
from agent_cli.integrations.export import (
    entries_to_adf,
    entries_to_html,
    entries_to_wiki,
)

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


# ── Wiki markup (Server / Data Center) ────────────────────────────────────────


class TestEntriesToWiki:
    def test_label_bold_and_body_follows(self):
        out = entries_to_wiki(
            [{"kind": "user", "label": "User", "body": "hi", "mono": False}]
        )
        assert "*User*" in out
        assert "hi" in out

    def test_mono_body_wrapped_in_code_block(self):
        out = entries_to_wiki(
            [{"kind": "observation", "label": "obs", "body": "x=1", "mono": True}]
        )
        assert "{code}\nx=1\n{code}" in out

    def test_empty_body_skipped(self):
        out = entries_to_wiki([{"kind": "user", "label": "User", "body": ""}])
        assert out == "*User*"

    def test_empty_entries_yields_placeholder(self):
        assert entries_to_wiki([]) == "(no content)"


# ── Jira instance resolution ─────────────────────────────────────────────────

_CFG = {
    "jira": {
        "instances": {
            "work": {"base_url": "https://work.atlassian.net"},
            "oss": {"base_url": "https://oss.atlassian.net/"},
            "dc": {"base_url": "https://jira.corp.net", "deployment": "server"},
        },
        "default": "work",
    }
}


class TestJiraResolution:
    def test_list_targets_names_and_deployment(self):
        targets = jira_mod.list_targets(_CFG)
        by_name = {t["name"]: t for t in targets}
        assert set(by_name) == {"work", "oss", "dc"}
        assert by_name["work"]["default"] is True
        # config-pinned deployment surfaces; unpinned is None (server probes it)
        assert by_name["dc"]["deployment"] == "server"
        assert by_name["work"]["deployment"] is None

    def test_list_targets_empty_when_unconfigured(self):
        assert jira_mod.list_targets({}) == []

    def test_resolve_default_no_credentials(self):
        inst = jira_mod.resolve_instance(_CFG)
        assert inst["name"] == "work"
        assert inst["base_url"] == "https://work.atlassian.net"
        # credentials are NOT resolved server-side anymore
        assert "api_token" not in inst and "email" not in inst
        assert inst["deployment"] is None

    def test_resolve_pinned_deployment_normalized(self):
        inst = jira_mod.resolve_instance(_CFG, "dc")
        assert inst["deployment"] == "server"

    def test_resolve_named_strips_trailing_slash(self):
        inst = jira_mod.resolve_instance(_CFG, "oss")
        assert inst["base_url"] == "https://oss.atlassian.net"  # no trailing /

    def test_resolve_unknown_raises(self):
        with pytest.raises(jira_mod.JiraError, match="Unknown Jira instance"):
            jira_mod.resolve_instance(_CFG, "nope")

    def test_resolve_no_config_raises(self):
        with pytest.raises(jira_mod.JiraError, match="No Jira instances"):
            jira_mod.resolve_instance({})

    def test_resolve_only_base_url_required(self):
        # base_url alone is now valid (credentials come from the request).
        cfg = {"jira": {"instances": {"x": {"base_url": "https://x"}}}, "default": "x"}
        inst = jira_mod.resolve_instance(cfg, "x")
        assert inst["base_url"] == "https://x"

    def test_resolve_missing_base_url_raises(self):
        cfg = {"jira": {"instances": {"x": {"deployment": "cloud"}}}, "default": "x"}
        with pytest.raises(jira_mod.JiraError, match="missing: base_url"):
            jira_mod.resolve_instance(cfg, "x")


class TestResolveTarget:
    def test_no_url_falls_back_to_config(self):
        inst = jira_mod.resolve_target(_CFG, "dc", None)
        assert inst["name"] == "dc"
        assert inst["base_url"] == "https://jira.corp.net"
        assert inst["deployment"] == "server"

    def test_url_matching_config_is_trusted(self):
        # trailing slash + config match → reuses the instance's deployment
        inst = jira_mod.resolve_target(_CFG, None, "https://jira.corp.net/")
        assert inst["name"] == "dc"
        assert inst["deployment"] == "server"

    def test_user_url_https_allowed_without_config(self):
        inst = jira_mod.resolve_target({}, None, "https://my.atlassian.net")
        assert inst["base_url"] == "https://my.atlassian.net"
        assert inst["name"] == "https://my.atlassian.net"
        assert inst["deployment"] is None

    def test_user_url_http_rejected(self):
        with pytest.raises(jira_mod.JiraError, match="https"):
            jira_mod.resolve_target({}, None, "http://insecure.example")

    def test_config_url_may_be_http_when_pinned(self):
        # a URL matching a configured instance is trusted as-is (admins may use
        # internal http); the https rule applies only to unconfigured URLs.
        cfg = {"jira": {"instances": {"int": {"base_url": "http://jira.lan"}}}}
        inst = jira_mod.resolve_target(cfg, None, "http://jira.lan")
        assert inst["name"] == "int"
        assert inst["base_url"] == "http://jira.lan"


# ── Deployment detection (mocked serverInfo) ──────────────────────────────────


class TestDetectDeployment:
    def setup_method(self):
        jira_mod._DEPLOYMENT_CACHE.clear()

    def _probe(self, url, status, payload):
        with patch("agent_cli.integrations.jira.requests.get") as get:
            get.return_value = MagicMock(
                status_code=status, json=MagicMock(return_value=payload)
            )
            return jira_mod.detect_deployment(url), get

    def test_cloud(self):
        result, get = self._probe(
            "https://x.atlassian.net", 200, {"deploymentType": "Cloud"}
        )
        assert result == "cloud"
        assert get.call_args.args[0].endswith("/rest/api/2/serverInfo")

    def test_server(self):
        result, _ = self._probe("https://jira.corp", 200, {"deploymentType": "Server"})
        assert result == "server"

    def test_unknown_payload_returns_none(self):
        result, _ = self._probe("https://jira.corp", 200, {"deploymentType": "Weird"})
        assert result is None

    def test_non_200_returns_none(self):
        result, _ = self._probe("https://jira.corp", 401, {})
        assert result is None

    def test_transport_error_returns_none(self):
        import requests

        with patch("agent_cli.integrations.jira.requests.get") as get:
            get.side_effect = requests.ConnectionError("down")
            assert jira_mod.detect_deployment("https://down") is None

    def test_success_is_cached(self):
        with patch("agent_cli.integrations.jira.requests.get") as get:
            get.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value={"deploymentType": "Cloud"}),
            )
            jira_mod.detect_deployment("https://x.atlassian.net")
            jira_mod.detect_deployment("https://x.atlassian.net")
        assert get.call_count == 1


# ── Jira POST (mocked) ───────────────────────────────────────────────────────


class TestPostComment:
    def test_cloud_posts_adf_to_v3_with_auth(self):
        adf = {"type": "doc", "version": 1, "content": []}
        with patch("agent_cli.integrations.jira.requests.post") as post:
            post.return_value = MagicMock(status_code=201, text="{}")
            url = jira_mod.post_comment(
                "https://work.atlassian.net", "cloud", "me@co.com", "tok", "PROJ-7", adf
            )
        assert url == "https://work.atlassian.net/browse/PROJ-7"
        call = post.call_args
        assert (
            call.args[0] == "https://work.atlassian.net/rest/api/3/issue/PROJ-7/comment"
        )
        assert call.kwargs["auth"] == ("me@co.com", "tok")
        assert call.kwargs["json"] == {"body": adf}

    def test_server_posts_wiki_string_to_v2_with_auth(self):
        with patch("agent_cli.integrations.jira.requests.post") as post:
            post.return_value = MagicMock(status_code=201, text="{}")
            url = jira_mod.post_comment(
                "https://jira.corp", "server", "alice", "pw", "DC-3", "*hi*"
            )
        assert url == "https://jira.corp/browse/DC-3"
        call = post.call_args
        assert call.args[0] == "https://jira.corp/rest/api/2/issue/DC-3/comment"
        assert call.kwargs["auth"] == ("alice", "pw")
        assert call.kwargs["json"] == {"body": "*hi*"}

    def test_none_deployment_defaults_to_cloud_v3(self):
        with patch("agent_cli.integrations.jira.requests.post") as post:
            post.return_value = MagicMock(status_code=201, text="{}")
            jira_mod.post_comment("https://x", None, "e", "t", "P-1", {})
        assert "/rest/api/3/" in post.call_args.args[0]

    def test_blank_issue_key_raises(self):
        with pytest.raises(jira_mod.JiraError, match="Issue key is required"):
            jira_mod.post_comment("https://x", "cloud", "e", "t", "  ", {})

    def test_jira_error_status_raises(self):
        with patch("agent_cli.integrations.jira.requests.post") as post:
            post.return_value = MagicMock(status_code=403, text="forbidden")
            with pytest.raises(jira_mod.JiraError, match="rejected the comment"):
                jira_mod.post_comment("https://x", "cloud", "e", "t", "P-1", {})

    def test_transport_error_raises(self):
        import requests

        with patch("agent_cli.integrations.jira.requests.post") as post:
            post.side_effect = requests.ConnectionError("down")
            with pytest.raises(jira_mod.JiraError, match="Could not reach Jira"):
                jira_mod.post_comment("https://x", "cloud", "e", "t", "P-1", {})
