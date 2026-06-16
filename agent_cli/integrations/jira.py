"""Jira comment export — instance resolution, deployment detection, REST POST.

Per-user identity
-----------------
Credentials are NOT stored server-side. A comment is posted as the FRONTEND
USER's own Jira account — their credentials are passed per-request from the
browser and used transiently for the single POST (never logged or persisted).
Config holds only the ``base_url`` (+ an optional explicit ``deployment``) per
named instance, so a user can target different Jira sites:

    "jira": {
        "instances": {
            "work": {"base_url": "https://work.atlassian.net"},
            "dc":   {"base_url": "https://jira.corp.net", "deployment": "server"}
        },
        "default": "work"
    }

Cloud vs Server/DC
------------------
The deployment type selects BOTH the REST API version and the comment-body
format:

- **Cloud** — ``/rest/api/3`` + an ADF document (structured JSON). Basic auth is
  ``email`` + ``API token`` (a password is not accepted by Cloud REST).
- **Server / Data Center** — ``/rest/api/2`` + a wiki-markup STRING. Basic auth
  is ``username`` + ``password`` (or a PAT).

When ``deployment`` is not pinned in config it is probed from
``{base_url}/rest/api/2/serverInfo`` (the ``deploymentType`` field), cached per
process. ``base_url`` is a plain argument to :func:`post_comment` so tests point
it at a local mock instead of a live (paid) Jira.
"""

from __future__ import annotations

from typing import Any

import requests

_TIMEOUT = 20
_PROBE_TIMEOUT = 10

# base_url (no trailing slash) -> "cloud" | "server". Probe results only; a
# failed/ambiguous probe is NOT cached so a later attempt can retry.
_DEPLOYMENT_CACHE: dict[str, str] = {}


class JiraError(Exception):
    """Config/resolution or transport error, surfaced to the user verbatim."""


def _normalize_deployment(value: Any) -> str | None:
    """Map a config/probe deployment string to ``"cloud"|"server"|None``.

    Atlassian's ``deploymentType`` is ``"Cloud"`` or ``"Server"``; Data Center
    self-reports as ``"Server"`` too, so both on-prem flavors collapse to
    ``"server"`` (same v2 API + wiki body)."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v == "cloud":
        return "cloud"
    if v in ("server", "datacenter", "data center"):
        return "server"
    return None


def _instances(config: dict[str, Any]) -> dict[str, Any]:
    jira = config.get("jira") or {}
    insts = jira.get("instances")
    if not isinstance(insts, dict) or not insts:
        raise JiraError(
            "No Jira instances configured. Add a 'jira.instances' section to "
            ".agent-cli/config.json (base_url per instance; credentials are "
            "entered per-user in the web UI)."
        )
    return insts


def list_targets(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Instance names + base URLs (+ config-pinned deployment) for the frontend.

    Pure: no network. ``deployment`` is the config-pinned value or ``None`` (the
    server endpoint fills ``None`` via :func:`detect_deployment`). Returns ``[]``
    (not an error) when nothing is configured so the UI can show "no Jira
    configured" rather than break.
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
                    "deployment": _normalize_deployment(inst.get("deployment")),
                }
            )
    return out


def detect_deployment(base_url: str) -> str | None:
    """Probe ``{base_url}/rest/api/2/serverInfo`` for the deployment type.

    Returns ``"cloud" | "server" | None`` (None = probe failed or ambiguous).
    ``serverInfo`` is typically reachable without auth and reports
    ``deploymentType`` directly, so this is a single unauthenticated GET.
    Successful results are cached per ``base_url``; failures are not cached.
    """
    base = base_url.rstrip("/")
    if base in _DEPLOYMENT_CACHE:
        return _DEPLOYMENT_CACHE[base]
    try:
        resp = requests.get(
            f"{base}/rest/api/2/serverInfo",
            headers={"Accept": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        result = _normalize_deployment((resp.json() or {}).get("deploymentType"))
    except (requests.RequestException, ValueError):
        return None
    if result:
        _DEPLOYMENT_CACHE[base] = result
    return result


def resolve_instance(
    config: dict[str, Any], target: str | None = None
) -> dict[str, Any]:
    """Resolve ``target`` (or ``jira.default``, or the sole instance) to its
    ``{name, base_url, deployment}``. Credentials are NOT resolved here — they
    come from the request. Raises :class:`JiraError` on an unknown target or a
    missing ``base_url``.
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
    if not inst.get("base_url"):
        raise JiraError(f"Jira instance {name!r} is missing: base_url.")
    return {
        "name": name,
        "base_url": str(inst["base_url"]).rstrip("/"),
        "deployment": _normalize_deployment(inst.get("deployment")),
    }


def resolve_target(
    config: dict[str, Any],
    target: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Resolve where to post — a config instance OR a user-supplied ``base_url``.

    When ``base_url`` is given it takes precedence (the UI lets a user type/edit
    the URL, optionally with no server config at all). A user-supplied URL that
    does NOT match any configured instance must be ``http://`` or ``https://``
    — the plaintext risk of ``http`` is surfaced as a UI warning, not blocked
    here. A URL matching a configured instance is trusted as-is (admins may use
    internal ``http``). With no ``base_url``, falls back to
    :func:`resolve_instance` (config-only path). Returns
    ``{name, base_url, deployment}``; raises :class:`JiraError` on a bad URL or
    an unresolvable config target.
    """
    user_url = (base_url or "").strip().rstrip("/")
    if not user_url:
        return resolve_instance(config, target)
    by_url = {t["base_url"].rstrip("/"): t for t in list_targets(config)}
    match = by_url.get(user_url)
    if match:
        return {
            "name": match["name"],
            "base_url": user_url,
            "deployment": match["deployment"],
        }
    if not user_url.lower().startswith(("http://", "https://")):
        raise JiraError(
            "Jira base URL must use http:// or https:// (or configure it server-side)."
        )
    return {"name": user_url, "base_url": user_url, "deployment": None}


def post_comment(
    base_url: str,
    deployment: str | None,
    auth_user: str,
    auth_secret: str,
    issue_key: str,
    body: Any,
) -> str:
    """POST a comment to ``{base_url}/rest/api/{2|3}/issue/{issue_key}/comment``.

    ``deployment`` selects the API version and the expected ``body`` shape:
    ``"server"`` → ``/rest/api/2`` with a wiki-markup STRING; anything else
    (``"cloud"``/``None``) → ``/rest/api/3`` with an ADF dict. Auth is HTTP Basic
    with the caller-supplied ``(auth_user, auth_secret)`` for both flavors —
    these are used only for this request and never stored. Returns the issue
    browse URL on success; raises :class:`JiraError` otherwise.
    """
    if not issue_key or not issue_key.strip():
        raise JiraError("Issue key is required (e.g. PROJ-123).")
    issue_key = issue_key.strip()
    base = base_url.rstrip("/")
    api_version = "2" if deployment == "server" else "3"
    url = f"{base}/rest/api/{api_version}/issue/{issue_key}/comment"
    try:
        resp = requests.post(
            url,
            auth=(auth_user, auth_secret),
            json={"body": body},
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
