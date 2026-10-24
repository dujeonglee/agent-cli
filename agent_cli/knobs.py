"""세션 노브 — 한 번 선언하고 표면들이 파생한다 (v10.43.0, docs/knobs/DESIGN.md).

세션 도중 바꿀 수 있고 다음 LLM 호출(또는 다음 확인·런)에 영향을 주는 값들이
"노브" 다. 종전엔 같은 노브가 세 표면(env 읽기·CLI 플래그·web API/칩)에 손으로
따로 배선돼 있었고, 그래서 셋(사고·문법·자동 승인)과 감사는 `run` 에 플래그가
없었다. 비대칭의 원인은 노브의 **정의가 어디에도 없었던 것** — 표면마다 자기
식으로 노브를 알고 있었다.

여기서는 `Knob` 이 선언이고 ``KNOBS`` 가 유일한 목록이다. 표면은 **표면당 하나**
씩만 적는다(노브 × 표면 어댑터가 아니다 — 그건 복사·붙여넣기가 옷만 갈아입는
것): `env_value`(env 표면), `knob_options`(CLI 표면 — typer 시그니처를 표에서
합성), 그리고 web 은 2단계에서 `/api/knobs` 하나로 온다. 노브는 typer·FastAPI·
sticky 를 모르고, 표면은 어떤 노브가 있는지 모른다. 새 노브는 선언 한 줄이다.

우선순위는 **CLI 인자 > env > 기본값**, 전부 세션 한정(재시작 시 다시 해석).
잘못된 CLI 값은 즉시 실패(`typer.BadParameter`), 잘못된 env 값은 기본값으로
떨어진다(종전 env 들의 동작 그대로 — 헤드리스에서 오타가 세션을 못 세우는 것과
조용히 기본값으로 가는 것 사이의 기존 결정).

상태 소유자는 흩어져 있다(ctx·registry·renderer·auditor). `KnobSession` 이 그
넷을 한 묶음으로 들고, 각 노브의 ``get``/``set`` 이 자기 소유자만 만진다.
"""

from __future__ import annotations

import inspect
import os
import typing
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

# ── 종류 — 문자열 ↔ 값 ───────────────────────────────────────────────────


class Kind:
    """노브 값의 종류: 파싱·검증·표시. 하위 클래스 셋뿐이다."""

    #: CLI 에서 값 없는 스위치 쌍(`--x/--no-x`)으로 받을지.
    is_flag = False

    def parse(self, text: Any) -> Any:  # pragma: no cover — 하위 클래스가 구현
        raise NotImplementedError

    def choices(self) -> tuple[str, ...] | None:
        return None

    def describe(self) -> str:
        """CLI help·칩 설명에 붙는 값 영역 한 토막."""
        return ""


class Text(Kind):
    """자유 문자열(모델 이름 등) — 검증은 소유자의 setter 가 한다."""

    def parse(self, text: Any) -> str:
        v = str(text).strip()
        if not v:
            raise ValueError("expected a non-empty value")
        return v

    def describe(self) -> str:
        return "name"


class Choice(Kind):
    def __init__(self, *choices: str) -> None:
        self._choices = tuple(choices)

    def parse(self, text: Any) -> str:
        v = str(text).strip().lower()
        if v not in self._choices:
            raise ValueError(
                f"expected one of {', '.join(self._choices)}; got {text!r}"
            )
        return v

    def choices(self) -> tuple[str, ...]:
        return self._choices

    def describe(self) -> str:
        return "|".join(self._choices)


class Bool(Kind):
    is_flag = True
    _ON = ("1", "true", "yes", "on")
    _OFF = ("0", "false", "no", "off")

    def parse(self, text: Any) -> bool:
        if isinstance(text, bool):
            return text
        v = str(text).strip().lower()
        if v in self._ON:
            return True
        if v in self._OFF:
            return False
        raise ValueError(f"expected on|off; got {text!r}")

    def describe(self) -> str:
        return "on|off"


class Number(Kind):
    """정수/실수. 범위 보정(clamp)은 소유자의 setter 가 한다 — 종전 그대로
    (`set_compaction_ratio`·`set_max_agents` 등이 clamp 를 소유).

    ``optional=True`` (v10.69.0, 🎲 샘플링): 값이 **없음**이 뜻을 가진다 — ``auto``
    ·빈 값·None 은 None 으로 파싱되고, 소유자는 None 을 "정하지 않음(키 삭제)" 으로
    받는다. 표의 기본값도 None 이라 부팅은 손대지 않는다."""

    def __init__(
        self, cast: Callable[[str], Any], *, unit: str = "", optional: bool = False
    ) -> None:
        self._cast = cast
        self._unit = unit
        self.optional = optional

    def parse(self, text: Any) -> Any:
        if self.optional and (
            text is None or str(text).strip().lower() in ("", "auto", "none", "null")
        ):
            return None
        if isinstance(text, (int, float)) and not isinstance(text, bool):
            return self._cast(text)
        try:
            return self._cast(str(text).strip())
        except ValueError:
            raise ValueError(f"expected a number; got {text!r}") from None

    def describe(self) -> str:
        return f"{self._unit} | auto" if self.optional else self._unit


class Duration(Kind):
    """`600`(초)·`10m`(분)·`0`(끔) — `constants.parse_duration` 과 같은 문법."""

    def parse(self, text: Any) -> int:
        if isinstance(text, int) and not isinstance(text, bool):
            return text
        from agent_cli.constants import parse_duration

        return parse_duration(str(text).strip())

    def describe(self) -> str:
        return "600 | 10m | 0"


# ── 상태 소유자 묶음 ─────────────────────────────────────────────────────


@dataclass
class KnobSession:
    """노브가 만지는 소유자들 — run/web 부트가 조립 뒤 한 번 만들고, web 은
    `/api/knobs`(2단계)가 같은 것을 든다. 없는 소유자는 None(테스트·부트 전)."""

    ctx: Any = None
    registry: Any = None
    renderer: Any = None
    auditor: Any = None
    binding: Any = None  # providers/binding.ModelBinding — 모델 노브의 소유자


# ── 선언 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KnobUI:
    """칩 모양 힌트 (3단계) — 데이터이지 코드가 아니다. 프런트의 렌더러 하나가 이걸로
    칩을 그린다: 새 노브는 선언 한 줄이면 칩이 생긴다."""

    icon: str = ""
    #: 같은 그룹은 한 칩(한 팝업)에 함께 — `KNOB_GROUPS` 가 아이콘·배지·설명을 가진다.
    group: str = ""
    #: "chip"(헤더 팝업 칩) | "header"(헤더의 토글 버튼 — 자동 승인)
    placement: str = "chip"
    #: 컨트롤 변형: "" 기본(종류대로) | "range"(숫자 슬라이더)
    #: | "unlimited"(0 = 무제한 체크박스) | "options"(서버가 준 options 의 셀렉트)
    control: str = ""
    #: 표시 배율 — 화면 값 = 저장 값 × scale (초→분은 1/60, 비율→% 는 100)
    scale: float = 1.0
    unit: str = ""
    #: 선택지의 표시 이름 (값 → 라벨)
    choice_labels: dict = field(default_factory=dict)


@dataclass(frozen=True)
class KnobGroup:
    icon: str
    label: str
    help: str
    badge: Callable[[KnobSession], str]
    note: Callable[[KnobSession], str] | None = None


@dataclass(frozen=True)
class Knob:
    """노브 하나. ``name`` 은 API·sticky 이름, ``flag`` 는 CLI, ``env`` 는 부팅
    env. ``get``/``set`` 은 `KnobSession` 의 소유자 하나만 만진다."""

    name: str
    flag: str
    env: str
    label: str
    help: str
    kind: Kind
    default: Any
    owner: str  # KnobSession 의 필드 이름 — 그 소유자 하나만 만진다
    get: Callable[[KnobSession], Any]
    set: Callable[[KnobSession, Any], Any]
    short: str = ""  # CLI 짧은 별칭 (`-m`)
    #: web 표면용 — 값 영역·파생 사실(범위, 지원 여부, 진행 수)과 잠김 사유.
    info: Callable[[KnobSession], dict] | None = None
    locked: Callable[[KnobSession], str | None] | None = None
    #: 칩 모양(3단계): 아이콘·그룹·자리·컨트롤 변형; 배지 글자·설명 한 줄·"켜짐" 강조는
    #: 서버가 계산한 문자열/불리언으로 간다 — 프런트는 보여주기만 한다.
    ui: KnobUI = field(default_factory=KnobUI)
    badge: Callable[[KnobSession], str] | None = None
    note: Callable[[KnobSession], str] | None = None
    on: Callable[[KnobSession], bool] | None = None

    def available(self, session: KnobSession) -> bool:
        """소유자가 서 있는가 — 없으면(headless 부트·세션 없음) 적용할 곳이 없다."""
        return getattr(session, self.owner, None) is not None

    def parse(self, text: Any) -> Any:
        return self.kind.parse(text)

    def env_value(self, environ: Mapping[str, str] | None = None) -> Any:
        """env 표면 — 미설정·빈 값·잘못된 값은 None(= 기본값으로)."""
        raw = (os.environ if environ is None else environ).get(self.env, "")
        if not str(raw).strip():
            return None
        try:
            return self.parse(raw)
        except ValueError:
            return None

    def resolve(self, cli_value: Any = None, environ=None) -> Any:
        """CLI 인자 > env > 기본값."""
        if cli_value is not None:
            return cli_value
        env_v = self.env_value(environ)
        return self.default if env_v is None else env_v


# ── 노브 — 소유자별 접근자 ───────────────────────────────────────────────


def _thinking_get(s: KnobSession) -> str:
    et = s.ctx.thinking_override.get("enable_thinking")
    return "auto" if et is None else ("on" if et else "off")


def _thinking_set(s: KnobSession, v: str) -> str:
    s.ctx.set_thinking_override(
        enable_thinking=None if v == "auto" else (v == "on"),
        reasoning_effort=s.ctx.thinking_override.get("reasoning_effort"),
    )
    return v


def _effort_get(s: KnobSession) -> str:
    return s.ctx.thinking_override.get("reasoning_effort") or "auto"


def _effort_set(s: KnobSession, v: str) -> str:
    s.ctx.set_thinking_override(
        enable_thinking=s.ctx.thinking_override.get("enable_thinking"),
        reasoning_effort=None if v == "auto" else v,
    )
    return v


def _grammar_get(s: KnobSession) -> str:
    ov = s.ctx.grammar_override
    return "auto" if ov is None else ("on" if ov else "off")


def _grammar_set(s: KnobSession, v: str) -> str:
    s.ctx.set_grammar_override(None if v == "auto" else (v == "on"))
    return v


def _caps(s: KnobSession):
    return s.binding.capabilities if s.binding is not None else None


def _thinking_info(s: KnobSession) -> dict:
    caps = _caps(s)
    return {"supports_thinking": None if caps is None else bool(caps.supports_thinking)}


def _thinking_locked(s: KnobSession) -> str | None:
    caps = _caps(s)
    if caps is not None and not caps.supports_thinking:
        return "this model does not support thinking"
    return None


def _grammar_info(s: KnobSession) -> dict:
    from agent_cli.context.manager import grammar_active

    caps = _caps(s)
    supported = caps.supports_grammar if caps is not None else None
    return {
        "supported": supported,
        "active": grammar_active(s.ctx.grammar_override, supported),
    }


def _grammar_locked(s: KnobSession) -> str | None:
    supported = _grammar_info(s)["supported"]
    if supported is False:
        return "the server cannot enforce a grammar for this model"
    if supported is None:
        return "grammar support for this model is not recorded in models.json"
    return None


def _model_info(s: KnobSession) -> dict:
    caps = _caps(s)
    return {
        "dialect": s.binding.dialect.name,
        "context_window": caps.context_window if caps is not None else None,
    }


def _model_options(s: KnobSession) -> list[str]:
    """models.json 에 등록되고 이 세션의 방언으로 풀리는 모델들 — `binding.switch`
    가 받는 것과 같은 규칙(`resolve_dialect` 가 `--dialect` 강제를 안다)."""
    from agent_cli.config import _load_registry
    from agent_cli.dialects import resolve_dialect

    out = []
    for name in sorted(_load_registry().get("models", {})):
        try:
            if resolve_dialect(name).name == s.binding.dialect.name:
                out.append(name)
        except KeyError:  # 방언 바인딩 없는 모델 — switch 도 거부한다
            continue
    return out


def _grammar_badge(s: KnobSession) -> str:
    info = _grammar_info(s)
    if info["supported"] is False:
        return "unsupported"
    if info["supported"] is None:
        return "unknown"
    return "enforced" if info["active"] else "off"


def _grammar_note(s: KnobSession) -> str:
    info = _grammar_info(s)
    if info["supported"] is False:
        return "The server cannot enforce a grammar — text protocol plus the recovery layer, as before."
    if info["supported"] is None:
        return "models.json does not record whether this model supports grammars — re-run model detection (probe) to fill it in."
    if info["active"]:
        return "Applies from the next request. Generation gets slower, but malformed calls, unknown tools and runaway bodies become impossible at the generation stage."
    return "No grammar is sent in this session."


def _auditor_badge(s: KnobSession) -> str:
    if s.auditor.in_flight > 0:
        return "auditing"
    return "on" if s.auditor.enabled else "off"


def _auditor_note(s: KnobSession) -> str:
    if s.auditor.in_flight > 0:
        return "An audit run is in progress — the result arrives as a card in the main channel."
    if s.auditor.enabled:
        return "From the next user run, every finished run is audited by a run that reads only that run's records."
    return "No auditing in this session."


def _minutes(seconds: int) -> int:
    return 0 if seconds <= 0 else max(1, round(seconds / 60))


def _stall_badge(s: KnobSession) -> str:
    m = _minutes(s.ctx.stream_idle_timeout_s)
    return "off" if m == 0 else f"{m}m×{s.ctx.stream_max_attempts}"


def _stall_note(s: KnobSession) -> str:
    m = _minutes(s.ctx.stream_idle_timeout_s)
    n = s.ctx.stream_max_attempts
    if m == 0:
        return "Stall detection is off — no resend"
    return f"{m} min × {n} attempts (first send included) = fails after at most {m * n} min"


def _num(v: float) -> str:
    """0.2 → "0.2", 1.0 → "1", 0.95 → "0.95" — 배지·설명용 짧은 숫자."""
    return f"{v:g}"


#: 🎲 노브 이름 → 배지의 짧은 글자 (`T 0.2 · p 0.95`).
_SAMPLING_SHORT = {"temperature": "T", "top_p": "p"}
#: auto 를 풀 때 칩이 먹일 값(실효값이 없을 때) — 둘 다 1.0 = "효과 없음" 이라
#: 체크를 푸는 순간 동작이 바뀌지 않는다.
_SAMPLING_PICK = {"temperature": 1.0, "top_p": 1.0}


def _sampling_get(name: str) -> Callable[[KnobSession], Any]:
    return lambda s: s.ctx.sampling.get(name)


def _sampling_set(name: str) -> Callable[[KnobSession, Any], Any]:
    return lambda s, v: s.ctx.set_sampling(name, v)


def _sampling_info(name: str) -> Callable[[KnobSession], dict]:
    """값 영역 + **지금 실제로 가는 값과 그 출처** — 칩은 보여주기만 한다."""

    def info(s: KnobSession) -> dict:
        from agent_cli.constants import SAMPLING_RANGES
        from agent_cli.providers.base import resolve_sampling, sampling_source

        lo, hi = SAMPLING_RANGES[name]
        caps = _caps(s)
        effective = source = None
        if caps is not None:
            effective = resolve_sampling(caps, s.ctx.sampling).get(name)
            source = sampling_source(caps, s.ctx.sampling, name)
        return {
            "min": lo,
            "max": hi,
            "step": 0.05 if name == "temperature" else 0.01,
            "effective": effective,
            "source": source,
            "pick": effective if effective is not None else _SAMPLING_PICK[name],
        }

    return info


def _sampling_badge(name: str) -> Callable[[KnobSession], str]:
    return lambda s: (
        "auto" if s.ctx.sampling.get(name) is None else _num(s.ctx.sampling[name])
    )


def _sampling_group_badge(s: KnobSession) -> str:
    parts = [
        f"{_SAMPLING_SHORT[n]} {_num(s.ctx.sampling[n])}"
        for n in _SAMPLING_SHORT
        if s.ctx.sampling.get(n) is not None
    ]
    return " · ".join(parts) if parts else "auto"


def _sampling_group_note(s: KnobSession) -> str:
    """지금 보내는 것을 키마다 출처와 함께 — 세션 값 / models.json 값 / 서버 기본."""
    from agent_cli.providers.base import resolve_sampling, sampling_source

    caps = _caps(s)
    if caps is None:
        return "Applies from the next LLM request."
    sent = resolve_sampling(caps, s.ctx.sampling)
    bits = []
    for n in _SAMPLING_SHORT:
        src = sampling_source(caps, s.ctx.sampling, n)
        if src == "session":
            bits.append(f"{n} {_num(sent[n])} (this session)")
        elif src == "models.json":
            bits.append(f"{n} {_num(sent[n])} from models.json")
        else:
            bits.append(f"{n}: server default")
    return "Now sent: " + " · ".join(bits) + ". Applies from the next LLM request."


def _thinking_badge(s: KnobSession) -> str:
    if _thinking_locked(s):
        return "unsupported"
    if _thinking_get(s) == "off":
        return "off"
    eff = _effort_get(s)
    return eff if eff != "auto" else "auto"


def _thinking_note(s: KnobSession) -> str:
    return (
        _thinking_locked(s)
        or "Applies from the next LLM request. Thinking off saves tokens; effort only when the backend honours it."
    )


#: 한 칩에 함께 그리는 노브 묶음 — 묶음의 아이콘·배지·설명은 여기서 계산한다.
KNOB_GROUPS: dict[str, KnobGroup] = {
    "stall": KnobGroup(
        icon="⏳",
        label="Stall",
        help=(
            "Server-stall watchdog — reconnect & resend after N minutes with no "
            "generated token (keep-alives don't count). 0 disables. Attempts is the "
            "TOTAL number of sends, first included; max wait = limit × attempts."
        ),
        badge=_stall_badge,
        note=_stall_note,
    ),
    "thinking": KnobGroup(
        icon="🧠",
        label="Thinking",
        help=(
            "Thinking and reasoning-effort control — applies from the next LLM "
            "request. Thinking off saves tokens and speeds generation up (when the "
            "backend supports it); reasoning effort counts only when the backend "
            "honours it. Session-scoped; auto on restart."
        ),
        badge=_thinking_badge,
        note=_thinking_note,
    ),
    "sampling": KnobGroup(
        icon="🎲",
        label="Sampling",
        help=(
            "Sampling parameters sent with every LLM request — temperature and "
            "top_p. auto (default) sends nothing: the model's entry in models.json "
            "decides, then the server default. Session-scoped; auto on restart."
        ),
        badge=_sampling_group_badge,
        note=_sampling_group_note,
    ),
}


def _model_set(s: KnobSession, v: str) -> str:
    error = s.binding.switch(v)
    if error:
        raise ValueError(error)
    return s.binding.model


KNOBS: tuple[Knob, ...] = (
    Knob(
        name="model",
        flag="--model",
        short="-m",
        env="AGENT_CLI_MODEL",
        label="Model",
        help=(
            "Model ID (default: config.json default_model, then the provider's "
            "default). Mid-session switches apply from the next LLM call; the "
            "model must be in models.json and bound to this session's dialect."
        ),
        kind=Text(),
        default=None,
        owner="binding",
        get=lambda s: s.binding.model,
        set=_model_set,
        ui=KnobUI(icon="🤖", control="options"),
        badge=lambda s: s.binding.model,
        note=lambda s: (
            f"dialect {s.binding.dialect.name} · window {_caps(s).context_window:,} tokens — only models bound to the same dialect can be selected."
            if _caps(s)
            else ""
        ),
        info=_model_info,
    ),
    Knob(
        name="thinking",
        owner="ctx",
        flag="--thinking",
        env="AGENT_CLI_THINKING",
        label="Thinking",
        help=(
            "Model thinking for this session: off (default — a model without a "
            "switch thinks forever), on, or auto (server/model default). Takes "
            "effect from the next LLM call; a model without thinking support "
            "ignores it."
        ),
        kind=Choice("off", "on", "auto"),
        default="off",
        get=_thinking_get,
        set=_thinking_set,
        ui=KnobUI(group="thinking"),
        info=_thinking_info,
        locked=_thinking_locked,
    ),
    Knob(
        name="reasoning_effort",
        owner="ctx",
        flag="--reasoning-effort",
        env="AGENT_CLI_REASONING_EFFORT",
        label="Reasoning effort",
        help="Reasoning effort when the backend honours it: auto (default), low, medium, high, off.",
        kind=Choice("auto", "low", "medium", "high", "off"),
        default="auto",
        get=_effort_get,
        set=_effort_set,
        ui=KnobUI(group="thinking"),
        # 사고 미지원 모델이면 노력도 뜻이 없다 — 🧠 칩의 두 셀렉트가 같이 잠긴다
        locked=_thinking_locked,
    ),
    Knob(
        name="grammar",
        owner="ctx",
        flag="--grammar",
        env="AGENT_CLI_GRAMMAR",
        label="Decoding grammar",
        help=(
            "Decoding grammar that forces the tool-call shape at generation: "
            "auto (default — on when models.json records support), on, off."
        ),
        kind=Choice("auto", "on", "off"),
        default="auto",
        get=_grammar_get,
        set=_grammar_set,
        ui=KnobUI(
            icon="📐",
            choice_labels={
                "auto": "on (default — when supported)",
                "on": "on (forced)",
                "off": "off",
            },
        ),
        badge=_grammar_badge,
        note=_grammar_note,
        on=lambda s: bool(_grammar_info(s)["active"]),
        info=_grammar_info,
        locked=_grammar_locked,
    ),
    Knob(
        name="temperature",
        owner="ctx",
        flag="--temperature",
        env="AGENT_CLI_TEMPERATURE",
        label="Temperature",
        help=(
            "Sampling temperature sent with every LLM request (0-2). auto "
            "(default) sends nothing — the model's models.json entry decides, "
            "then the server default. Applies from the next LLM call."
        ),
        kind=Number(float, unit="0-2", optional=True),
        default=None,
        get=_sampling_get("temperature"),
        set=_sampling_set("temperature"),
        ui=KnobUI(group="sampling", control="optional", unit="0–2"),
        badge=_sampling_badge("temperature"),
        on=lambda s: s.ctx.sampling.get("temperature") is not None,
        info=_sampling_info("temperature"),
    ),
    Knob(
        name="top_p",
        owner="ctx",
        flag="--top-p",
        env="AGENT_CLI_TOP_P",
        label="Top-p",
        help=(
            "Nucleus sampling top_p sent with every LLM request (0-1). auto "
            "(default) sends nothing — the model's models.json entry decides, "
            "then the server default. Applies from the next LLM call."
        ),
        kind=Number(float, unit="0-1", optional=True),
        default=None,
        get=_sampling_get("top_p"),
        set=_sampling_set("top_p"),
        ui=KnobUI(group="sampling", control="optional", unit="0–1"),
        badge=_sampling_badge("top_p"),
        on=lambda s: s.ctx.sampling.get("top_p") is not None,
        info=_sampling_info("top_p"),
    ),
    Knob(
        name="max_agents",
        owner="registry",
        flag="--max-agents",
        env="AGENT_CLI_MAX_AGENTS",
        label="Agent cap",
        help="Max concurrently live sub-agents; 0 = unlimited (default 10).",
        kind=Number(int, unit="count"),
        default=10,
        get=lambda s: s.registry.max_agents,
        set=lambda s, v: s.registry.set_max_agents(v),
        ui=KnobUI(icon="👥", control="unlimited"),
        badge=lambda s: (
            "∞" if s.registry.max_agents == 0 else str(s.registry.max_agents)
        ),
        info=lambda s: {"min": 1, "unlimited": s.registry.max_agents == 0},
    ),
    Knob(
        name="compaction_ratio",
        owner="ctx",
        flag="--compaction-ratio",
        env="AGENT_CLI_COMPACTION_RATIO",
        label="Compaction ratio",
        help=(
            "Context compaction target as a fraction of the window (0.5-1.0). "
            "1.0 (default) = compact only when a generation is cut by the window; "
            "lower = compact earlier."
        ),
        kind=Number(float, unit="0.5-1.0"),
        default=1.0,
        get=lambda s: s.ctx.compaction_ratio,
        set=lambda s, v: s.ctx.set_compaction_ratio(v),
        ui=KnobUI(icon="🗜️", control="range", scale=100, unit="%"),
        badge=lambda s: f"{round(s.ctx.compaction_ratio * 100)}%",
        info=lambda s: {"min": 0.5, "max": 1.0, "step": 0.05},
    ),
    Knob(
        name="stall",
        owner="ctx",
        flag="--stall",
        env="AGENT_CLI_STREAM_IDLE_TIMEOUT_S",
        label="Stream stall limit",
        help=(
            "Stream stall limit before re-sending: 600 (seconds), 10m (minutes), "
            "0 (off). Default 10m."
        ),
        kind=Duration(),
        default=600,
        get=lambda s: s.ctx.stream_idle_timeout_s,
        set=lambda s, v: s.ctx.set_stream_idle_timeout(v),
        ui=KnobUI(group="stall", scale=1 / 60, unit="min · 0 = off"),
        info=lambda s: {"min": 0, "max": 3600, "step": 60},
    ),
    Knob(
        name="stall_attempts",
        owner="ctx",
        flag="--stall-attempts",
        env="AGENT_CLI_STREAM_MAX_ATTEMPTS",
        label="Stall attempts",
        help=(
            "TOTAL send attempts on stall, first send included (1-10). "
            "Max wait = --stall x this. Default 4."
        ),
        kind=Number(int, unit="1-10"),
        default=4,
        get=lambda s: s.ctx.stream_max_attempts,
        set=lambda s, v: s.ctx.set_stream_max_attempts(v),
        ui=KnobUI(group="stall", unit="attempts"),
        # 한도 0 = 감지 끔 → 시도 횟수라는 개념이 성립하지 않는다 (v8.60.0 규칙)
        locked=lambda s: (
            "stall detection is off (limit 0)"
            if s.ctx.stream_idle_timeout_s <= 0
            else None
        ),
        info=lambda s: {"min": 1, "max": 10},
    ),
    Knob(
        name="auto_approve",
        owner="renderer",
        flag="--auto-approve",
        env="AGENT_CLI_AUTO_APPROVE",
        label="Auto-approve",
        help=(
            "Skip every confirmation prompt (dangerous shell commands, path "
            "guards) and allow — on the terminal there is nobody to ask. "
            "Default off."
        ),
        kind=Bool(),
        default=False,
        get=lambda s: bool(s.renderer.auto_approve),
        set=lambda s, v: s.renderer.set_auto_approve(v),
        ui=KnobUI(icon="⚡", placement="header"),
        badge=lambda s: "no confirmations",
        on=lambda s: bool(s.renderer.auto_approve),
    ),
    Knob(
        name="auditor",
        owner="auditor",
        flag="--auditor",
        env="AGENT_CLI_AUDITOR",
        label="Session audit",
        help=(
            "Audit every finished user run with a harness job that reads only "
            "that run's records and reports per-request verdicts to main "
            "(docs/auditor/DESIGN.md). Default off."
        ),
        kind=Bool(),
        default=False,
        get=lambda s: bool(s.auditor.enabled),
        set=lambda s, v: setattr(s.auditor, "enabled", bool(v)) or bool(v),
        ui=KnobUI(icon="🔎", choice_labels={"off": "off (default)", "on": "on"}),
        badge=_auditor_badge,
        note=_auditor_note,
        on=lambda s: bool(s.auditor.enabled),
        info=lambda s: {"in_flight": s.auditor.in_flight},
    ),
)

KNOB_BY_NAME: dict[str, Knob] = {k.name: k for k in KNOBS}


# ── web 표면 — 한 모양 (`/api/knobs`, sticky `knob`) ────────────────────


def web_state(knob: Knob, session: KnobSession) -> dict:
    """칩·`/api/knobs`·sticky 가 같이 읽는 노브 상태 한 장. 소유자가 없으면
    ``available: false`` 에 값 없음."""
    available = knob.available(session)
    state: dict = {
        "name": knob.name,
        "label": knob.label,
        "help": knob.help,
        "flag": knob.flag,
        "env": knob.env,
        "kind": type(knob.kind).__name__.lower(),
        "choices": knob.kind.choices(),
        "default": knob.default,
        "available": available,
        "value": knob.get(session) if available else None,
        "locked": None,
    }
    state["ui"] = asdict(knob.ui)
    state["badge"] = None
    state["note"] = ""
    state["on"] = False
    state["group"] = None
    if available:
        if knob.locked is not None:
            state["locked"] = knob.locked(session)
        if knob.info is not None:
            state.update(knob.info(session))
        if knob.ui.control == "options":
            state["options"] = _model_options(session)
        state["badge"] = knob.badge(session) if knob.badge else str(state["value"])
        state["note"] = knob.note(session) if knob.note else ""
        state["on"] = bool(knob.on(session)) if knob.on else False
        if knob.ui.group:
            state["group"] = web_group(knob.ui.group, session)
    return state


def web_group(name: str, session: KnobSession) -> dict:
    """묶음 칩의 아이콘·배지·설명 — 멤버 어느 쪽이 바뀌어도 같이 실린다."""
    g = KNOB_GROUPS[name]
    members = [k for k in KNOBS if k.ui.group == name]
    available = all(k.available(session) for k in members)
    return {
        "name": name,
        "icon": g.icon,
        "label": g.label,
        "help": g.help,
        "members": [k.name for k in members],
        "badge": g.badge(session) if available else None,
        "note": (g.note(session) if g.note and available else ""),
    }


def web_states(session: KnobSession) -> list[dict]:
    return [web_state(k, session) for k in KNOBS]


def table_spec() -> list[dict]:
    """표의 정적 선언 — `agent-cli knobs --json` 이 보드 같은 바깥 쪽에 준다.
    값·소유자 없이 이름·플래그·env·종류·선택지·기본값·모양 힌트만."""
    return [
        {
            "name": k.name,
            "label": k.label,
            "help": k.help,
            "flag": k.flag,
            "short": k.short,
            "env": k.env,
            "kind": type(k.kind).__name__.lower(),
            "choices": list(k.kind.choices() or ()) or None,
            "default": k.default,
            # 묶음 노브(🧠 사고·노력, ⏳ 한도·시도)의 아이콘은 묶음 것 — 바깥(보드)은
            # KNOB_GROUPS 를 모르므로 행마다 채워 준다
            "ui": {
                **asdict(k.ui),
                "icon": k.ui.icon
                or (KNOB_GROUPS[k.ui.group].icon if k.ui.group else ""),
            },
        }
        for k in KNOBS
    ]


# ── 부팅 — CLI > env > 기본값을 소유자에 적용 ────────────────────────────


def resolve_all(cli_values: Mapping[str, Any] | None = None, environ=None) -> dict:
    """모든 노브의 부팅값 ``{name: value}``."""
    cli_values = cli_values or {}
    return {k.name: k.resolve(cli_values.get(k.name), environ) for k in KNOBS}


def apply_all(session: KnobSession, values: Mapping[str, Any]) -> None:
    """부팅값을 소유자들에 적용한다 — 소유자 넷이 모두 선 뒤, 그리고 상주
    에이전트 복원·auto-spawn **전에**(상한이 그 게이트다)."""
    for knob in KNOBS:
        value = values.get(knob.name)
        # None = 값 없음(모델: config/프로바이더 기본) — 소유자가 없어도 건너뛴다.
        if value is not None and knob.available(session):
            knob.set(session, value)


# ── CLI 표면 — 표에서 typer 옵션을 합성한다 ─────────────────────────────


def knob_options(fn: Callable) -> Callable:
    """`run`/`web` 에 노브 플래그 전부를 붙이는 데코레이터 — 두 명령이 같은
    표에서 같은 플래그를 받으니 어긋날 수 없다. 꾸며진 함수는 ``knob_args``
    키워드 하나로 ``{name: parsed_value}``(준 것만)를 받는다. 잘못된 값은
    여기서 `typer.BadParameter` — 세션을 세우기 전에 실패한다."""
    import typer

    # `from __future__ import annotations` 모듈의 함수는 주석이 문자열이다 —
    # typer 는 `get_type_hints` 로 푸는데 합성 시그니처엔 그 길이 없으니 여기서
    # 풀어 실제 타입을 박는다.
    hints = typing.get_type_hints(fn)
    sig = inspect.signature(fn)
    base = [
        p.replace(annotation=hints.get(p.name, p.annotation))
        for p in sig.parameters.values()
        if p.name != "knob_args"
    ]
    extra = []
    for knob in KNOBS:
        if knob.kind.is_flag:
            default = typer.Option(
                None, f"{knob.flag}/--no-{knob.flag[2:]}", help=knob.help
            )
            ann: Any = bool | None
        else:
            hint = knob.kind.describe()
            names = [knob.flag] + ([knob.short] if knob.short else [])
            default = typer.Option(
                None, *names, help=f"{knob.help} [{hint}; env {knob.env}]"
            )
            ann = str | None
        extra.append(
            inspect.Parameter(
                knob.name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=ann,
            )
        )

    def wrapper(*args, **kwargs):
        knob_args: dict[str, Any] = {}
        for knob in KNOBS:
            raw = kwargs.pop(knob.name, None)
            if raw is None:
                continue
            try:
                knob_args[knob.name] = knob.parse(raw)
            except ValueError as exc:
                raise typer.BadParameter(f"{knob.flag}: {exc}") from None
        return fn(*args, knob_args=knob_args, **kwargs)

    wrapper.__signature__ = sig.replace(parameters=base + extra)  # type: ignore[attr-defined]
    wrapper.__annotations__ = {p.name: p.annotation for p in base + extra}
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__module__ = fn.__module__
    return wrapper
