"""Jira Cloud comment export — instance resolution + REST POST.

Config (``config.json``) supports MULTIPLE named instances so a user can target
different Jira sites (work / OSS / …):

    "jira": {
        "instances": {
            "work": {"base_url": "https://work.atlassian.net",
                     "email": "me@co.com", "api_token": "…"},
            "oss":  {"base_url": "https://oss.atlassian.net",
                     "email": "me@x.com",  "api_token": "…"}
        },
        "default": "work"
    }

API tokens stay server-side: :func:`list_targets` returns only names + base
URLs for the frontend dropdown; the POST is made by the server with the
resolved credentials. ``base_url`` is a plain argument to :func:`post_comment`
so tests point it at a local mock instead of a live (paid) Jira.
"""

from __future__ import annotations

from typing import Any

import requests

_TIMEOUT = 20


class JiraError(Exception):
    """Config/resolution or transport error, surfaced to the user verbatim."""


def _instances(config: dict[str, Any]) -> dict[str, Any]:
    jira = config.get("jira") or {}
    insts = jira.get("instances")
    if not isinstance(insts, dict) or not insts:
        raise JiraError(
            "No Jira instances configured. Add a 'jira.instances' section to "
            ".agent-cli/config.json (base_url / email / api_token per instance)."
        )
    return insts


def list_targets(config: dict[str, Any]) -> list[dict[str, str]]:
    """Instance names + base URLs for the frontend dropdown — NO tokens.

    Returns ``[]`` (not an error) when nothing is configured, so the UI can
    simply show "no Jira configured" rather than break.
    """
    jira = config.get("jira") or {}
    insts = jira.get("instances")
    if not isinstance(insts, dict):
        return []
    default = jira.get("default")
    out = []
    for name, inst in insts.items():
        if isinstance(inst, dict) and inst.get("base_url"):
            out.append(
                {
                    "name": name,
                    "base_url": str(inst["base_url"]),
                    "default": name == default,
                }
            )
    return out


def resolve_instance(
    config: dict[str, Any], target: str | None = None
) -> dict[str, str]:
    """Resolve ``target`` (or ``jira.default``, or the sole instance) to its
    ``{name, base_url, email, api_token}``. Raises :class:`JiraError` on an
    unknown target or missing required fields.
    """
    insts = _instances(config)
    default = (config.get("jira") or {}).get("default")
    name = target or default or (next(iter(insts)) if len(insts) == 1 else None)
    if not name:
        raise JiraError(
            f"Ambiguous Jira target: configure 'jira.default' or pass one of "
            f"{sorted(insts)}."
        )
    inst = insts.get(name)
    if not isinstance(inst, dict):
        raise JiraError(f"Unknown Jira instance {name!r}. Configured: {sorted(insts)}.")
    missing = [k for k in ("base_url", "email", "api_token") if not inst.get(k)]
    if missing:
        raise JiraError(f"Jira instance {name!r} is missing: {', '.join(missing)}.")
    return {
        "name": name,
        "base_url": str(inst["base_url"]).rstrip("/"),
        "email": str(inst["email"]),
        "api_token": str(inst["api_token"]),
    }


def post_comment(
    base_url: str, email: str, api_token: str, issue_key: str, adf_body: dict
) -> str:
    """POST an ADF comment to ``{base_url}/rest/api/3/issue/{issue_key}/comment``.

    Jira Cloud uses Basic auth (email + API token) and an ADF ``body``. Returns
    the issue browse URL on success; raises :class:`JiraError` otherwise.
    """
    if not issue_key or not issue_key.strip():
        raise JiraError("Issue key is required (e.g. PROJ-123).")
    issue_key = issue_key.strip()
    base = base_url.rstrip("/")
    url = f"{base}/rest/api/3/issue/{issue_key}/comment"
    try:
        resp = requests.post(
            url,
            auth=(email, api_token),
            json={"body": adf_body},
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise JiraError(f"Could not reach Jira: {e}") from e
    if resp.status_code not in (200, 201):
        detail = resp.text[:300]
        raise JiraError(
            f"Jira rejected the comment ({resp.status_code}) for {issue_key}: {detail}"
        )
    return f"{base}/browse/{issue_key}"
