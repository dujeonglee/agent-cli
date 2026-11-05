"""Prompt inventory — everything the harness tells a model, read off the code.

``python -m agent_cli.prompts.inventory -o inventory.json``
``python -m agent_cli.prompts.inventory --html inventory.html``

A hand-written inventory goes stale with the next prompt change. This one is
assembled by the same functions the loop calls, so it is what a model would
see at the commit it ran on:

* ``dialects`` — per dialect and per scenario (:data:`SCENARIOS`), the system
  prompt as named sections with a token estimate each, and for a
  server-parsed dialect the function schemas that travel in the request.
* ``tools`` — per built-in tool: description, parameters, and its guide.
* ``runtime`` — texts injected mid-run (nudges, rejections, tail blocks).
  These live outside the prompt builder, so they are a hand-kept list
  (:func:`_runtime_texts`); a text added elsewhere has to be added here.

``--html`` writes the same data as one self-contained page (the JSON is
embedded in ``inventory.html``'s data slot; no network, no build step) to
browse it: pick a dialect and a scenario, open a section.

Some sections depend on where it runs (Environment, and the project's own
skills / profiles / DIRECTIVE.md) — the output records ``cwd`` for that
reason. Run it from the repository root for the built-in baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from agent_cli import __version__, dialects
from agent_cli.context.render import estimate_tokens
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.registry import (
    _BUILTIN_ORDER,
    TOOLS,
    effective_tool_names,
    strip_depth_gated,
)

#: Depth ceiling used for every scenario (the CLI default is the same order).
_MAX_DEPTH = 3


class _NoAgents:
    """What the prompt builder reads from an agent registry, with nobody
    spawned — the main loop's prompt at the start of a session."""

    max_agents = 5

    def roster_snapshot(self) -> list:
        return []

    def get(self, key):
        return None


@dataclass(frozen=True)
class Scenario:
    """One loop situation the prompt differs by."""

    key: str
    title: str
    tools: tuple[str, ...] | None  # None = every built-in tool
    depth: int
    has_registry: bool
    session_dir: str = ""


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("main", "Main loop — all tools, can spawn agents", None, 0, True),
    Scenario("subagent", "Sub-agent loop — all tools, run only", None, 1, False),
    Scenario(
        "narrowed",
        "Sub-agent with a narrowed tool list (read_file, shell)",
        ("read_file", "shell"),
        1,
        False,
    ),
    Scenario(
        "limit",
        "Loop at the depth ceiling — no further nesting",
        None,
        _MAX_DEPTH,
        False,
    ),
)


def _caps() -> ModelCapabilities:
    return ModelCapabilities(
        context_window=32768, max_output_tokens=4096, supports_thinking=False
    )


def _scenario(dialect, sc: Scenario) -> dict:
    from agent_cli.prompts.system_prompt import (
        build_system_prompt_sections,
        function_schemas_for,
    )

    tools = list(sc.tools) if sc.tools is not None else list(_BUILTIN_ORDER)
    # 루프 조립과 같은 판정 — 상한 시나리오엔 `run_skill`·`agent` 와 그 절이 없다
    tools = strip_depth_gated(tools, sc.depth, _MAX_DEPTH)
    registry = _NoAgents() if sc.has_registry else None
    sections = [
        {"name": name, "tokens": estimate_tokens(text), "text": text}
        for name, text in build_system_prompt_sections(
            _caps(),
            tools,
            dialect=dialect,
            depth=sc.depth,
            max_depth=_MAX_DEPTH,
            agent_registry=registry,
        )
    ]
    out: dict = {
        "title": sc.title,
        "tools": effective_tool_names(tools, dialect),
        "system_tokens": sum(s["tokens"] for s in sections),
        "sections": sections,
    }
    if getattr(dialect, "server_parsed", False):
        functions = function_schemas_for(
            tools, dialect, has_agent_registry=sc.has_registry
        )
        out["functions"] = [
            {
                "name": f["function"]["name"],
                "tokens": estimate_tokens(json.dumps(f, ensure_ascii=False)),
                "description": f["function"]["description"],
                "parameters": f["function"]["parameters"],
            }
            for f in functions
        ]
        out["functions_tokens"] = sum(f["tokens"] for f in out["functions"])
    return out


def _tools(dialect) -> list[dict]:
    """The built-in tools as the main loop is told about them."""
    from agent_cli.prompts.system_prompt import _build_tool_inline_guides

    names = list(_BUILTIN_ORDER)
    guides = _build_tool_inline_guides(names, dialect)
    out = []
    for name in names:
        tool = TOOLS[name]
        guide = (guides.get(name) or "").strip()
        out.append(
            {
                "name": name,
                "description": tool.description,
                "description_tokens": estimate_tokens(tool.description),
                "parameters": tool.parameters,
                "guide": guide,
                "guide_tokens": estimate_tokens(guide) if guide else 0,
            }
        )
    return out


def _wait_checkin_sample() -> str:
    """`wait` 중간 깨우기 알림의 고정 문장들 (v10.79.0, `runtime.wait_checkin`) —
    머리줄·선택지 문장을 자리 표시자로 채운 표본. 상태 줄(작업·monitor·에이전트)은
    사실 그대로라 표기 규칙의 대상이 아니다."""
    from agent_cli.runtime import (
        CHECKIN_AGAIN,
        CHECKIN_AGENT_STUCK,
        CHECKIN_HEAD,
        CHECKIN_QUESTION,
        CHECKIN_STOP,
        CHECKIN_STOP_MONITOR,
    )

    return "\n".join(
        [
            CHECKIN_HEAD.format(span="5m"),
            "<one line per running job, monitor or agent>",
            " ".join(
                [
                    CHECKIN_AGAIN.format(subj="it is", span="10m"),
                    CHECKIN_STOP.format(stops="`kill -- -<pid>`"),
                    CHECKIN_STOP_MONITOR,
                    CHECKIN_AGENT_STUCK,
                    CHECKIN_QUESTION,
                ]
            ),
        ]
    )


class _TwoAgents:
    """What the tail's ``## Live Agents`` block reads off the registry: one agent
    working with a queued request and one dead, at the cap — so the block carries
    every sentence it can (follow-up, how to wait, dead·resume, limit)."""

    max_agents = 1

    class _Instance:
        description = "Read-only code analyst — explains how code works."

    def roster_snapshot(self) -> list:
        return [
            {
                "key": "agt-1",
                "profile": "code-analyst",
                "name": "auth",
                "state": "busy",
                "pending_requests": 1,
            },
            {
                "key": "agt-2",
                "profile": "code-writer",
                "name": "",
                "state": "dead",
                "pending_requests": 0,
            },
        ]

    def get(self, key):
        return self._Instance() if key == "agt-1" else None


def _agent_result_samples() -> list[tuple[str, str, str]]:
    """`agent` 결과·거절 문구 표본 (v10.85.0) — 상수를 자리 표시자로 채운 것.
    도구 결과는 inventory 의 다른 경로(설명·절)를 거치지 않아 여기서 등록한다;
    표기 가드가 이 표본을 본다."""
    from agent_cli.prompts.system_prompt import build_live_agents_section
    from agent_cli.subagent import agents_live as al
    from agent_cli.tools.agent_tool import REGISTRY_MODES

    return [
        (
            "live_agents_tail",
            "The tail's `## Live Agents` block while agents exist (main loop)",
            build_live_agents_section(_TwoAgents(), include_state=True),
        ),
        (
            "live_agents_peer_roster",
            "The same block as a resident agent sees it (peers, `message`)",
            build_live_agents_section(
                _TwoAgents(), exclude_key="agt-2", via_message_tool=True
            ),
        ),
        (
            "agent_spawn_result",
            "`agent` spawn accepted with a first task while a dead agent of the same profile exists",
            "\n".join(
                [
                    al.spawn_head("agt-3", ["code-writer", "api"]),
                    al.SPAWN_DEAD_SAME_PROFILE.format(keys=al.quoted_keys(["agt-2"])),
                    al.SPAWN_TASK_QUEUED.format(rid="r-4"),
                    al.SPAWN_PEERS.format(who=al.peer_address("api", "agt-3")),
                ]
            ),
        ),
        (
            "agent_spawn_without_task",
            "`agent` spawn accepted without a task — the last line instead",
            al.SPAWN_NO_TASK.format(key="agt-3"),
        ),
        (
            "agent_request_result",
            "`agent` request queued (second line only when two or more are queued)",
            al.REQUEST_QUEUED.format(key="agt-1", rid="r-5")
            + al.REQUEST_STACKED.format(n=2),
        ),
        (
            "agent_resume_result",
            "`agent` resume accepted, with a task",
            "\n".join(
                [
                    al.RESUME_DONE.format(key="agt-2"),
                    al.RESUME_TASK_QUEUED.format(rid="r-6"),
                ]
            ),
        ),
        (
            "agent_kill_result",
            "`agent` kill — first time, then again on the same key",
            "\n".join(
                [al.KILL_DONE.format(key="agt-1"), al.KILL_ALREADY.format(key="agt-1")]
            ),
        ),
        (
            "agent_status_extras",
            "`agent` status: a dead agent's line suffix, and the nudge while one is working",
            al.STATUS_DEAD_SUFFIX.strip() + "\n" + al.STATUS_WAIT_NUDGE,
        ),
        *_value_quoting_samples(),
        (
            "message_delivered",
            "`message` accepted — as a request, then as a notice",
            al.delivered_line("r-7", expects_reply=True)
            + "\n"
            + al.delivered_line("n-3", expects_reply=False),
        ),
        (
            "agent_spawn_refused_duplicate",
            "`agent` spawn refused: the same profile·name exists dead / alive",
            "\n".join(
                [
                    al.DUPLICATE_DEAD.format(label="code-writer · api", key="agt-2"),
                    al.DUPLICATE_ALIVE.format(label="code-writer · api", key="agt-3"),
                ]
            ),
        ),
        (
            "agent_modes_refused_in_subloop",
            "A loop without a registry (sub-agent) asked for a persistent mode",
            al.NO_REGISTRY.format(
                modes="/".join(REGISTRY_MODES), hint=al.NO_REGISTRY_PEER_HINT
            ),
        ),
    ]


def _value_quoting_samples() -> list[tuple[str, str, str]]:
    """경로·ref 를 값으로 싣는 문구 — 큰 출력 안내(저장본·원본 파일)와 줄 번호만 온 ref.
    표기 가드(값은 큰따옴표)가 이 모양을 보게 표본으로 둔다."""
    from agent_cli.tools.base import oversized_nudge
    from agent_cli.tools.edit_file import _bare_ref_error

    body = "\n".join(f"line {i}" for i in range(12000))
    tools = frozenset(["read_file", "agent"])
    saved = oversized_nudge(
        tool_name="shell",
        path=".agent-cli/tmp/out-3.txt",
        body=body,
        tokens=90000,
        cap=8000,
        retry_hint="",
        tools_available=tools,
    )
    source = oversized_nudge(
        tool_name="read_file",
        path="src/big.c",
        body=body,
        tokens=90000,
        cap=8000,
        retry_hint="",
        tools_available=tools,
        path_is_source=True,
    )
    bare = _bare_ref_error({"pos": "2", "end": "30"}, ["a", "b", "c"], "app.py") or ""
    return [
        ("oversized_saved", "A tool result over the cap — saved to a file", saved),
        ("oversized_source", "`read_file` over the cap — the file itself", source),
        ("edit_bare_line_ref", "`edit_file` ref given as a bare line number", bare),
    ]


def _browser_result_samples() -> list[tuple[str, str, str]]:
    """`browser` 결과·거절 문구 표본 (v10.106.0) — ``agent_cli.browser`` 의 모듈 상수와
    작은 함수를 가짜 인자로 채운 것. 도구 결과라 다른 경로로는 inventory 에 안 실린다;
    표기 가드가 이 표본을 본다. playwright 없이 돈다(문구만 쓴다)."""
    from agent_cli import browser as br

    url = "https://shop.test/"
    return [
        (
            "browser_same_page_changed",
            "`browser` action that changed the tree in place (header and snapshot follow)",
            br.SAME_PAGE_CHANGED.format(what='selected "North" in combobox "Branch"'),
        ),
        (
            "browser_same_page_unchanged",
            "`browser` action after which the accessibility tree is the same",
            br.SAME_PAGE_UNCHANGED.format(what="pressed ArrowLeft", url=url),
        ),
        (
            "browser_snapshot_cut",
            "Tail of a snapshot cut to the auto-snapshot budget",
            br.SNAPSHOT_CUT.format(depth=2, shown=36, total=640, tokens=17000),
        ),
        (
            "browser_ref_errors",
            "`browser` refused a `ref`: malformed, unknown, stale, not actionable",
            "\n".join(
                [
                    br.BAD_REF.format(ref="Search"),
                    br.UNKNOWN_REF.format(ref="e99"),
                    br.STALE_REF.format(ref="e12"),
                    br.NOT_ACTIONABLE.format(ref="e4", role="heading"),
                ]
            ),
        ),
        (
            "browser_page_errors",
            "`browser` called with no page, after `close`, after the window closed",
            f"{br.NO_PAGE}\n{br.BROWSER_CLOSED}\n{br.WINDOW_CLOSED}",
        ),
        (
            "browser_header_lines",
            "Lines under a snapshot header: new window, verification pending / passed",
            (
                f"{br.WINDOW_LOST_LINE}\n{br.VERIFICATION_PENDING_LINE}\n"
                f"{br.VERIFICATION_PASSED_LINE}"
            ),
        ),
        (
            "browser_no_match",
            "`browser` find with no match",
            br.NO_MATCH.format(pattern="zzz", url=url),
        ),
        (
            "browser_challenge",
            (
                "A page that asks for human verification: window visible / opened / "
                "none possible, then an action refused on it"
            ),
            "\n".join(
                [
                    br.challenge_notice(
                        url, br.is_challenge(url, "Just a moment…", ""), shown=shown
                    )
                    for shown in ("visible", "opened")
                ]
                + [
                    br.challenge_notice(
                        url,
                        "unusual-traffic block",
                        shown="blocked",
                        failed="AGENT_CLI_BROWSER_HEADLESS=1",
                    ),
                    br.challenge_refusal("unusual-traffic block", visible=False),
                    br.challenge_refusal("unusual-traffic block", visible=True),
                ]
            ),
        ),
    ]


class _TwoSchedules:
    """`schedule` 목록 표본용 가짜 레지스트리 — 켠 예약 하나(놓친 발화 있음)와 꺼 둔
    예약 하나. ``list_all``·``next_fire`` 만 흉내 낸다."""

    def __init__(self):
        from agent_cli.schedule.registry import Schedule

        def sched(sid, cron, prompt, label, enabled, missed=None):
            return Schedule(
                id=sid,
                source="agent",
                cron=cron,
                prompt=prompt,
                label=label,
                nickname="",
                enabled=enabled,
                created_at="2026-11-01T08:00:00",
                settled_at="2026-11-01T08:00:00",
                missed_at=missed,
            )

        self._all = [
            sched(
                "1a2b3c4d",
                "0 9 * * 1",
                "weekly report",
                "Weekly",
                True,
                missed="2026-11-03T09:00:00",
            ),
            sched("5e6f7a8b", "*/30 * * * *", "check the queue", "", False),
        ]

    def list_all(self):
        return list(self._all)

    def next_fire(self, s):
        from datetime import datetime

        # 예약은 현지 시각 기준(naive) — 레지스트리와 같은 모양으로 고정 시각을 준다
        return datetime.fromisoformat("2026-11-10T09:00") if s.enabled else None


def _schedule_result_samples() -> list[tuple[str, str, str]]:
    """`schedule` 결과·거절 문구 표본 (v10.108.0) — ``tools.schedule`` 와
    ``schedule.registry`` 의 모듈 상수를 가짜 인자로 채운 것. 도구 결과라 다른
    경로로는 inventory 에 안 실린다; 표기 가드가 이 표본을 본다."""
    from agent_cli.schedule import cron
    from agent_cli.schedule import registry as sr
    from agent_cli.tools import schedule as st

    listing = st._fmt_schedules(_TwoSchedules())
    try:  # 파서의 실제 사유를 싣는다
        cron.parse("99 9 * * *")
        bad_cron = ""
    except ValueError as e:
        bad_cron = str(e)
    return [
        (
            "schedule_added",
            "`schedule` add accepted — the listing follows",
            f"{st.SCHEDULED.format(id='1a2b3c4d')}\n{listing}",
        ),
        (
            "schedule_deleted_empty",
            "`schedule` delete accepted, nothing left",
            f"{st.DELETED}\n{st.NO_SCHEDULES}",
        ),
        (
            "schedule_add_rejected",
            "`schedule` add refused: bad cron, empty prompt, the per-session cap",
            "\n".join(
                st.ADD_REJECTED.format(why=why)
                for why in (
                    sr.INVALID_CRON.format(cron="99 9 * * *", why=bad_cron),
                    sr.EMPTY_PROMPT,
                    sr.CAP_REACHED.format(cap=sr.AGENT_CAP),
                )
            ),
        ),
        (
            "schedule_delete_rejected",
            "`schedule` delete with an unknown `id` — the listing follows",
            f"{st.DELETE_REJECTED.format(sid='nope')}\n{listing}",
        ),
        (
            "schedule_unavailable",
            "`schedule` called with no active session",
            st.UNAVAILABLE,
        ),
    ]


def _tool_help_samples(dialect) -> list[tuple[str, str, str]]:
    """`tool_help` 결과 표본 (v10.109.0) — main 루프와 같은 묶음(이 방언·레지스트리
    있음)으로 `monitor` 의 전문 한 건과 모르는 이름 한 건. 표기 가드가 본다."""
    from agent_cli.prompts.system_prompt import tool_guide_text
    from agent_cli.tools.base import RunContext

    names = list(_BUILTIN_ORDER)
    ctx = RunContext(
        tools_available=frozenset(names),
        tool_guide=lambda n: tool_guide_text(n, names, dialect),
    )
    help_tool = TOOLS["tool_help"]
    full = help_tool._run({"name": "monitor"}, ctx=ctx)
    unknown = help_tool._run({"name": "grep"}, ctx=ctx)
    return [
        ("tool_help_result", "`tool_help` for `monitor`", full.output or ""),
        (
            "tool_help_unknown",
            "`tool_help` with a name not in this loop",
            unknown.error,
        ),
    ]


def _runtime_texts(dialect) -> list[dict]:
    """Texts the harness injects mid-run. Hand-kept: add an entry when a new
    model-facing text is introduced outside the system prompt."""
    from agent_cli.constants import AUDIT_REQUEST
    from agent_cli.context.manager import _OBS_COMPLETE_NUDGE
    from agent_cli.loop.dispatch import BATCH_OP_SKIPPED_NOTE
    from agent_cli.prompts.session_state import (
        COMPACTION_NOTICE,
        COMPACTION_NOTICE_WITHOUT_MEMORY,
        RULES_HEADER,
        SESSION_STATE_HEADER,
        TAIL_BOUNDARY,
    )
    from agent_cli.recovery.common_recovery import format_action_loop_intervention
    from agent_cli.recovery.dialect_recovery import (
        format_no_action_retry,
        format_no_json_retry,
    )
    from agent_cli.recovery.recursion import (
        format_depth_limit_error,
        format_recursion_error,
    )
    from agent_cli.tools.base import GENERIC_RETRY_HINT

    prior = "<the model's rejected output>"
    entries: list[tuple[str, str, str]] = [
        (
            "obs_complete_nudge",
            "Appended to the newest tool result",
            _OBS_COMPLETE_NUDGE,
        ),
        (
            "session_state_header",
            "Opens the session-state block at the tail of the last message",
            SESSION_STATE_HEADER,
        ),
        ("rules_header", "Opens the standing-rules part of that block", RULES_HEADER),
        ("tail_boundary", "Separates the tail block from the message", TAIL_BOUNDARY),
        ("compaction_notice", "First turn after a compaction", COMPACTION_NOTICE),
        (
            "compaction_notice_without_memory",
            "Same, in a loop without the memory tool",
            COMPACTION_NOTICE_WITHOUT_MEMORY,
        ),
        (
            "audit_request",
            "The task the harness gives the auditor job after a user run",
            AUDIT_REQUEST.format(
                a=12,
                b=40,
                session="1791",
                history="/w/.agent-cli/sessions/1791/history.jsonl",
            ),
        ),
        (
            "format_nudge_no_call",
            "The turn had no parseable call",
            format_no_json_retry(prior_content=prior, dialect=dialect).message,
        ),
        (
            "format_nudge_no_action",
            "A call was parsed but named no tool",
            format_no_action_retry(prior_content=prior, dialect=dialect).message,
        ),
        (
            "batch_op_skipped",
            "A rejected op inside a multi-op turn",
            BATCH_OP_SKIPPED_NOTE,
        ),
        (
            "oversized_retry_hint",
            "A tool result too large to show (tools may override the hint)",
            GENERIC_RETRY_HINT,
        ),
        (
            "recursion_error",
            "A skill or agent calls itself through its own stack",
            format_recursion_error("skill", "review", ["plan", "review"]),
        ),
        (
            "depth_limit_error",
            "Nesting reached the depth ceiling",
            format_depth_limit_error("agent", "worker", _MAX_DEPTH, _MAX_DEPTH),
        ),
        (
            "wait_checkin",
            "An accepted `wait` has not been woken for 5m (then 10·20·40·60m)",
            _wait_checkin_sample(),
        ),
        *_agent_result_samples(),
        *_browser_result_samples(),
        *_schedule_result_samples(),
        *_tool_help_samples(dialect),
        (
            "agent_message_field",
            "`agent` called with `message` instead of `task`",
            TOOLS["agent"].validate({"mode": "spawn", "message": "review it"}) or "",
        ),
        (
            "agent_task_no_words",
            "`agent` called with a `task` that has no words",
            TOOLS["agent"].validate({"mode": "spawn", "task": "\n>\n"}) or "",
        ),
    ]
    for level in (1, 2):
        intervention = format_action_loop_intervention(
            level=level,
            action="shell",
            args_repr='{"command": "ls"}',
            repeat_count=level + 1,
            task="<the user's request>",
            prior_result="<what that call returned the previous time>",
        )
        if intervention is not None:
            entries.append(
                (
                    f"action_loop_level_{level}",
                    f"The same call repeated (escalation level {level})",
                    intervention.message,
                )
            )
    return [
        {"id": id_, "when": when, "tokens": estimate_tokens(text), "text": text}
        for id_, when, text in entries
    ]


def build_inventory() -> dict:
    """The whole inventory as plain data (JSON-serialisable)."""
    out: dict = {
        "agent_cli_version": __version__,
        "cwd": os.getcwd(),
        "token_estimate": "agent_cli.context.render.estimate_tokens",
        "scenarios": {sc.key: sc.title for sc in SCENARIOS},
        "dialects": {},
    }
    for name in sorted(dialects.list_names()):
        dialect = dialects.get(name)
        out["dialects"][name] = {
            "server_parsed": bool(getattr(dialect, "server_parsed", False)),
            "scenarios": {sc.key: _scenario(dialect, sc) for sc in SCENARIOS},
            "tools": _tools(dialect),
            "runtime": _runtime_texts(dialect),
        }
    return out


#: Where the page template keeps the data (inside a JSON ``<script>``).
_DATA_SLOT = "__INVENTORY_DATA__"


def render_html(inventory: dict) -> str:
    """The inventory as one self-contained HTML page.

    The data goes into a ``<script type="application/json">`` element, whose
    content the browser never parses as markup — except for a closing
    ``</script>``, so ``</`` is written ``<\\/`` (still valid JSON)."""
    template = Path(__file__).with_name("inventory.html").read_text(encoding="utf-8")
    data = json.dumps(inventory, ensure_ascii=False).replace("</", "<\\/")
    return template.replace(_DATA_SLOT, data, 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_cli.prompts.inventory",
        description="Write everything the harness tells a model, as JSON or a page.",
    )
    parser.add_argument(
        "-o", "--output", default="-", help="output file ('-' = stdout, the default)"
    )
    parser.add_argument(
        "--html",
        metavar="FILE",
        help="write a self-contained page to browse the inventory instead of JSON",
    )
    args = parser.parse_args(argv)
    inventory = build_inventory()
    if args.html:
        Path(args.html).write_text(render_html(inventory), encoding="utf-8")
        return 0
    text = json.dumps(inventory, ensure_ascii=False, indent=2) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
