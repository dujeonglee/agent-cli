"""Model capabilities: what each model supports."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import requests

from agent_cli.config import get_model_entry, save_model_entry
from agent_cli.constants import DETECTION_PROBE_TIMEOUT
from agent_cli.thinking_tags import THINK_TAG_NAMES as _THINKING_TAGS

# Optional progress callback — set by the caller (main.py) before
# runtime capability detection so the user sees what each probe step
# is doing. Cold-load + two probes can take 20-30s for a large model
# the first time; without progress the CLI appears frozen. Default
# None keeps capabilities.py callable in isolation (tests, scripts).
_progress_cb: Callable[[str], None] | None = None


def set_progress_callback(cb: Callable[[str], None] | None) -> None:
    """Register (or clear) a progress callback. Called once by main.py
    around the get_capabilities() invocation that might hit runtime
    detection. Pass None to disable."""
    global _progress_cb
    _progress_cb = cb


def _emit_progress(msg: str) -> None:
    """Emit a single progress message through the registered callback,
    if any. Safe no-op when no callback is set."""
    cb = _progress_cb
    if cb is not None:
        try:
            cb(msg)
        except Exception:
            # Never let a progress UI error derail capability detection.
            pass


@dataclass(frozen=True)
class ModelCapabilities:
    # v7.0.0: supports_structured_output / supports_strict_schema 필드 제거 —
    # 유일 소비자(react 의 json_mode)가 react 와 함께 제거돼 소비자 0.
    # 구 models.json 엔트리의 해당 키는 로더가 무시 (무해).
    context_window: int
    max_output_tokens: int
    supports_thinking: bool
    #: 서버가 디코딩 문법(``guided_grammar``)을 **강제**하는가 (v9.24.0).
    #: None = 아직 모름(등록 엔트리에 키가 없음) — 첫 사용 때 한 번 프로브해
    #: 프로세스 안에서 채운다. 사용자가 적은 엔트리는 파일에 다시 쓰지 않는다.
    supports_grammar: bool | None = None
    #: 모델이 이미지를 **보는가** (v10.59.0, docs/vision/DESIGN.md). None = 아직
    #: 모름(등록 엔트리에 키가 없음) — 이미지를 처음 보낼 때 한 번 프로브해
    #: 프로세스 안에서 채운다(:func:`vision_support`). 사용자가 적은 엔트리는
    #: 파일에 다시 쓰지 않는다 (supports_grammar 와 같은 정책).
    supports_vision: bool | None = None
    #: 이 모델의 **기본 샘플링** (v10.69.0, docs/knobs/DESIGN.md §7) — models.json
    #: 엔트리의 선택 키 ``temperature``·``top_p``. None = 미기록(필드를 보내지 않아
    #: 서버 기본). 세션 🎲 노브가 정하지 않은 키에만 쓰인다(`providers.base.
    #: resolve_sampling`). **프로브는 채우지 않는다** — 사용자·보드만 적는 값이다.
    temperature: float | None = None
    top_p: float | None = None


# Fallback for a model we could neither look up (models.json) nor probe
# (runtime detection returned None). The window is OPTIMISTIC on purpose:
# the old 4096 made ``oversized_cap`` (= window/10) 409 tokens, so virtually
# every read_file / shell observation was dropped as over-cap — the agent was
# unusable on any model that failed detection, and 4096 also contradicted
# ``MIN_CONTEXT_WINDOW`` (detection hard-rejects anything under 16K). 256K
# errs the other way: on a model whose real window is smaller the first call
# is rejected by the server and ``ContextManager.force_fit`` (flow 2) shrinks
# and retries — a few wasted round trips instead of a crippled session.
# ``max_output_tokens`` stays conservative (a too-large request is rejected
# outright by many servers, with no reactive recovery path).
DEFAULT_CONTEXT_WINDOW = 262_144  # 256K
DEFAULT_CAPABILITIES = ModelCapabilities(
    context_window=DEFAULT_CONTEXT_WINDOW,
    max_output_tokens=2048,
    supports_thinking=False,
)

# Auto-detected output token budget = context_window // this. A model
# with a 256K window gets 64K output; 16K → 4K. (Registry/models.json
# entries keep their stored value — this only governs detection.)
_OUTPUT_TOKEN_DIVISOR = 4

# Models with a context window below this are rejected at detection
# time: even output (= window//4) would be under 4K, too small to run
# the agent loop usefully. Surfaced as UnsupportedModelError so the CLI
# can fail fast with a clear message instead of silently degrading.
MIN_CONTEXT_WINDOW = 16384


class UnsupportedModelError(RuntimeError):
    """Raised during capability detection when a model's context window
    is too small (< MIN_CONTEXT_WINDOW) to run the agent. Propagates out
    of get_capabilities; the CLI catches it and exits with a message."""


# Context-window detection fallback when neither /v1/models metadata nor
# the overflow probe yields a number. 128K is a realistic floor for
# modern local models — far less wasteful than the old 4096 default,
# while staying under-set (safe: flow 2 overflow recovery corrects at
# runtime if the real window is smaller). See docs/ARCHITECTURE.md.
_DEFAULT_CONTEXT_FALLBACK = 131072  # 128K

# Filler size for the overflow probe (in repetitions of "word "). At
# ~0.75 tokens/word this is ≈1.5M tokens — over the limit of essentially
# every local model, so the server rejects it right after tokenisation
# (no eval/generation, no server occupancy). Models whose real window
# exceeds ~1.5M simply return 200 and the caller falls back.
_CONTEXT_PROBE_WORDS = 2_000_000


# Tracks whether the last get_capabilities() call triggered runtime detection
_last_was_runtime_detected: bool = False


def was_runtime_detected() -> bool:
    """Return True if the last get_capabilities() call used runtime detection."""
    return _last_was_runtime_detected


def get_capabilities(
    model: str,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str = "",
) -> ModelCapabilities:
    """Look up capabilities with priority: models.json > runtime detection > defaults."""
    global _last_was_runtime_detected
    _last_was_runtime_detected = False

    # Priority 1: Static registry (models.json)
    entry = get_model_entry(model)
    if entry is not None:
        # 등록 엔트리에 `supports_grammar` 가 없으면 None(미확인) 그대로 —
        # 부트에서 프로브하지 않는다. 프로브는 모델 감지(`_detect_capabilities`)
        # 때 한 번 판정해 저장하는 것: 부트마다 모델에 요청 하나를 내면 서버가
        # 바쁠 때(벤치 중 실측 54초) 인스턴스 열기가 그만큼 늦어진다.
        return _build_from_entry(entry)

    # Priority 2: Runtime detection
    if provider and base_url:
        detected = _detect_runtime_capabilities(provider, base_url, model, api_key)
        if detected is not None:
            _auto_save_detected(model, detected)
            _last_was_runtime_detected = True
            return detected

    # Priority 3: Conservative defaults
    return DEFAULT_CAPABILITIES


def caps_to_entry(caps: ModelCapabilities, *, auto_detected: bool = False) -> dict:
    """Serialize a ``ModelCapabilities`` to a models.json entry dict — the
    inverse of :func:`_build_from_entry`. Single source so the field list can't
    drift between the runtime-probe save path and the setup-wizard save path
    (``main._prompt_model_capabilities``). ``auto_detected`` tags entries
    written by the runtime probe."""
    entry = {
        "context_window": caps.context_window,
        "max_output_tokens": caps.max_output_tokens,
        "supports_thinking": caps.supports_thinking,
    }
    if caps.supports_grammar is not None:
        entry["supports_grammar"] = caps.supports_grammar
    if caps.supports_vision is not None:
        entry["supports_vision"] = caps.supports_vision
    # 기본 샘플링은 값이 있을 때만 — 프로브 산출이 아니므로 자동 감지 엔트리에는
    # 키가 생기지 않는다(v10.69.0).
    if caps.temperature is not None:
        entry["temperature"] = caps.temperature
    if caps.top_p is not None:
        entry["top_p"] = caps.top_p
    if auto_detected:
        entry["_auto_detected"] = True
    return entry


def _auto_save_detected(model: str, caps: ModelCapabilities) -> None:
    """Save runtime-detected capabilities to ~/.agent-cli/models.json (new models only)."""
    save_model_entry(model, caps_to_entry(caps, auto_detected=True))


def _build_from_entry(entry: dict) -> ModelCapabilities:
    # Legacy field `supports_tool_calling` — silently ignored if present in
    # older models.json entries; the loop uses ReAct text parsing, not the
    # native tool-calling API on any provider.
    sg = entry.get("supports_grammar")
    sv = entry.get("supports_vision")
    return ModelCapabilities(
        context_window=entry.get("context_window", DEFAULT_CONTEXT_WINDOW),
        max_output_tokens=entry.get("max_output_tokens", 2048),
        supports_thinking=entry.get("supports_thinking", False),
        supports_grammar=bool(sg) if sg is not None else None,
        supports_vision=bool(sv) if sv is not None else None,
        temperature=_optional_float(entry.get("temperature")),
        top_p=_optional_float(entry.get("top_p")),
    )


def _optional_float(value) -> float | None:
    """엔트리의 선택 숫자 키 — 없음·숫자 아님은 None(미기록으로 취급, 경고 없음:
    손으로 적은 파일의 오타가 부트를 막지 않는다)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: ``(base_url, model) → bool`` — 프로세스 안 비전 프로브 결과 (한 번만 묻는다).
_vision_probed: dict[tuple[str, str], bool] = {}


def vision_support(
    caps: ModelCapabilities,
    transport_factory,
    base_url: str,
    model: str,
    api_key: str = "",
) -> bool:
    """모델이 이미지를 보는가 — 등록값이 있으면 그것, 없으면(``None``) 한 번
    프로브해 프로세스 안에 기억하고, **판정이 났으면 models.json 의 빠진 키에
    적는다** (v10.68.0, `config.fill_model_entry`).

    프로브는 이미지를 **보내 보고 답을 검사**한다(두 색을 순서대로): 조각을
    조용히 버리는 서버(omlx 의 tool 메시지 실측)에서 "200 이면 참" 은 거짓
    양성이라서다. 어떤 실패도 False — 없는 능력을 약속하지 않는다.

    영속은 **판정이 난 경우만**: 답이 돌아왔거나(맞든 틀리든) 서버가 4xx 로 이미지를
    거부한 경우. 연결 실패·타임아웃·5xx 는 이 프로세스에서만 False 다 — 서버가
    바쁜 순간의 False 를 파일에 박으면 다음부터 영영 못 본다.
    """
    if caps.supports_vision is not None:
        return caps.supports_vision
    key = (base_url.rstrip("/"), model)
    hit = _vision_probed.get(key)
    if hit is None:
        settled = True
        try:
            hit = bool(transport_factory(base_url, model, api_key).vision_probe())
        except requests.HTTPError as exc:
            hit = False
            status = getattr(getattr(exc, "response", None), "status_code", 0) or 0
            settled = 400 <= status < 500
        except Exception:
            hit = False
            settled = False
        _vision_probed[key] = hit
        if settled:
            from agent_cli.config import fill_model_entry

            fill_model_entry(model, {"supports_vision": hit})
    return hit


def vision_known(caps: ModelCapabilities, base_url: str, model: str) -> bool | None:
    """지금 **아는** 값만 — 등록값, 없으면 이 프로세스가 이미 프로브한 결과,
    둘 다 없으면 ``None``. 프로브하지 않는다 (v10.60.0): 웹 📎 가 부팅마다
    모델 서버에 요청을 보내지 않기 위해 — 값은 첫 이미지 런이 정한다."""
    if caps.supports_vision is not None:
        return caps.supports_vision
    return _vision_probed.get((base_url.rstrip("/"), model))


#: 문법 프로브의 정답 — 서버가 문법을 강제하면 이것만 나올 수 있다.
_GRAMMAR_PROBE_ANSWER = "GRAMMAR-OK"


def probe_grammar_support(
    provider: str, base_url: str, model: str, api_key: str = ""
) -> bool:
    """서버가 ``guided_grammar`` 를 **강제**하는지 한 번 묻는다 (v9.24.0).

    "지원한다" 는 필드를 받아 준다는 뜻이 아니라 출력이 문법에 묶인다는
    뜻이다 — 모르는 필드를 조용히 버리는 서버(실측: omlx 는 ``guided_regex``
    를 그렇게 버린다)가 있어 응답 본문으로 판정한다. 어떤 실패도 False.
    """
    try:
        if provider == "openai":
            t = _OpenAITransport(base_url, model, api_key)
            return t.grammar_probe()
    except Exception:
        return False
    return False


# Compiled patterns for efficiency. 태그 vocab 은 thinking_tags 단일 소스
# (모듈 상단 import — strip 경로와 탐지 경로가 같은 4종을 봐야 한다).
_THINKING_TAG_PATTERN = re.compile(
    r"<(" + "|".join(_THINKING_TAGS) + r")>",
    re.IGNORECASE,
)


def _detect_runtime_capabilities(
    provider: str, base_url: str, model: str, api_key: str = ""
) -> ModelCapabilities | None:
    """Detect model capabilities at runtime via provider API.

    All providers share the probe orchestration (`_detect_capabilities`);
    transport 는 **프로바이더 클래스의 ``capability_transport`` 훅** 소유
    (v8.41.0 self-register — 종전 이 함수의 provider-이름 분기 흡수).
    미등록 프로바이더/훅 부재는 None → 기본값 폴백 (종전 계약)."""
    from agent_cli.providers import get_provider_class

    cls = get_provider_class(provider)
    transport_factory = getattr(cls, "capability_transport", None)
    if transport_factory is None:
        return None
    return _detect_capabilities(model, transport_factory(base_url, model, api_key))


# Prompt whose natural answer is prose, not JSON. If the server does not
# enforce ``response_format`` it answers with markdown, which fails
# ``json.loads`` — so a passing probe means the constraint was genuinely
# honored, not that the model happened to emit JSON on its own.


def _detect_thinking(content: str) -> bool:
    """``<think>``-style tag detection on a probe response's content."""
    return bool(_THINKING_TAG_PATTERN.search(content or ""))


def _detect_capabilities(model: str, transport) -> ModelCapabilities | None:
    """Provider-agnostic capability-probe orchestration.

    The PROVIDER-SPECIFIC ``transport`` supplies three calls — the only part
    that differs between OpenAI and Anthropic (endpoint / headers / request +
    response shape):
      - ``context_window()`` — 3-tier (metadata → overflow probe → fallback).
      - ``thinking_probe()`` → bool. **공통 계약: 2단계** — ① 기본 프로브(모델이
        기본으로 사고를 뱉으면 감지) → ② 미검출 시 방언별 "사고 켜기" 스위치로
        재프로브(OpenAI: chat_template_kwargs.enable_thinking / Anthropic:
        thinking 블록). 재프로브 실패는 관용(→ 미지원) — 기본 OFF 사고 모델
        (Qwen 등)을 transport 무관하게 잡는다.
    Everything else (reject-too-small, ``max_output = ctx//4``, assembly,
    error→None degradation) is shared. ``UnsupportedModelError`` (window below
    the minimum) propagates; any other probe failure returns None so detection
    degrades to defaults rather than crashing."""
    try:
        _emit_progress(f"First run for {model} — detecting capabilities")
        _emit_progress(f"Querying context window ({model})")
        context_window = transport.context_window()

        # Thinking probe runs before the reject (matches the historical order).
        # 2단계(기본 → 사고-켜기 재프로브, transport 공통 계약)라 기본 OFF 사고
        # 모델(Qwen 등)도 잡는다 — 미검출이면 재프로브만큼 더 걸릴 수 있다.
        _emit_progress(
            f"Probing thinking support ({model}) — may take ~10s on cold load"
        )
        supports_thinking = transport.thinking_probe()

        if context_window < MIN_CONTEXT_WINDOW:
            raise UnsupportedModelError(
                f"{model}: context window {context_window:,} is below the "
                f"{MIN_CONTEXT_WINDOW:,} minimum — too small to run the agent."
            )
        max_output = context_window // _OUTPUT_TOKEN_DIVISOR

        _emit_progress(f"Probing decoding-grammar support ({model})")
        try:
            supports_grammar = bool(transport.grammar_probe())
        except Exception:
            supports_grammar = False

        # 비전 (v10.59.0): 이미지를 보내 두 색을 맞히는지 본다. 실패는 관용
        # (→ 미지원) — 텍스트 모델에 이미지를 보내면 400 이 흔하다.
        _emit_progress(f"Probing image (vision) support ({model})")
        try:
            supports_vision = bool(transport.vision_probe())
        except Exception:
            supports_vision = False

        _emit_progress(f"Detection complete for {model}")
        return ModelCapabilities(
            context_window=context_window,
            max_output_tokens=max_output,
            supports_thinking=supports_thinking,
            supports_grammar=supports_grammar,
            supports_vision=supports_vision,
        )
    except UnsupportedModelError:
        # Hard reject — propagate to the CLI instead of degrading to defaults.
        raise
    except Exception as e:
        import sys

        print(f"[warn] capability detection failed for {model}: {e}", file=sys.stderr)
        return None


class _ProbeTransport:
    """capability 프로브 전송의 공통 골격 (v10.114.0).

    프로바이더와 무관한 흐름은 여기 한 곳이 소유한다:
      - ``context_window()`` 3단 — ``/models`` 메타데이터(``max_model_len`` →
        ``context_length``) → 상한 초과 프로브의 400 본문 파싱 →
        ``_DEFAULT_CONTEXT_FALLBACK``.
      - ``simple_chat()`` — 사고(필드·블록)를 ``<think>…</think>content`` 로
        정규화해 태그 검출 하나로 합친다.
      - ``thinking_probe()`` 2단 · ``vision_probe()``.

    서브클래스는 **모양만** 정한다: ``chat_url``·``_auth_headers()``·
    ``_body()``·``_text()``·``_thinking()``·``_image_part()``·``grammar_probe()``.
    요청 URL·헤더·JSON 본문은 통합 전(v10.113.x)의 두 전송·모듈 함수가 보내던
    것과 같다 — 옛 코드와 나란히 돌려 기록을 대조해 확인했다."""

    #: ``self.base`` 뒤에 붙는 채팅 끝점 경로 — 서브클래스가 정한다.
    chat_path: str = ""

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    @property
    def chat_url(self) -> str:
        return f"{self.base}{self.chat_path}"

    # ─── 프로바이더별 모양 (서브클래스) ───────────────────────────

    def _auth_headers(self) -> dict:
        """인증·버전 헤더 — GET ``/models`` 는 이것만 보낸다(Content-Type 없음)."""
        raise NotImplementedError

    def _body(
        self,
        content,
        max_tokens: int,
        *,
        enable_thinking: bool = False,
        probe: bool = False,
    ) -> dict:
        """한 사용자 메시지짜리 요청 본문. ``content`` 는 문자열 또는 조각 목록.
        ``enable_thinking`` 은 방언의 사고 켜기 스위치, ``probe`` 는 판정용
        프로브(비전·문법)가 덧붙이는 결정성 필드."""
        raise NotImplementedError

    def _text(self, data) -> str:
        """응답 본문의 답 텍스트."""
        raise NotImplementedError

    def _thinking(self, data) -> str:
        """응답 본문의 사고 텍스트(없으면 빈 문자열)."""
        raise NotImplementedError

    def _image_part(self, mime: str, b64: str) -> dict:
        raise NotImplementedError

    def grammar_probe(self) -> bool:
        raise NotImplementedError

    # ─── 공통 ────────────────────────────────────────────────────

    def _post_headers(self) -> dict:
        return {"Content-Type": "application/json", **self._auth_headers()}

    def _post(self, body: dict):
        return requests.post(
            self.chat_url,
            json=body,
            headers=self._post_headers(),
            timeout=DETECTION_PROBE_TIMEOUT,
        )

    def context_window(self) -> int:
        """3단: 메타데이터 → 상한 초과 프로브 → ``_DEFAULT_CONTEXT_FALLBACK``
        (128K — 낮게 잡아 그 자체로 400 을 내지 않고, 실제가 더 작으면 flow 2
        런타임 복구가 고친다)."""
        ctx = self._metadata_window()
        if ctx:
            return ctx
        # 메타데이터에 없다(omlx 등, 실 Anthropic 도) — 상한 초과로 묻는다.
        _emit_progress(f"Probing context window via overflow ({self.model})")
        probed = self._overflow_window()
        if probed:
            return probed
        return _DEFAULT_CONTEXT_FALLBACK

    def _metadata_window(self) -> int | None:
        """``GET {base}/models`` 에서 이 모델 항목의 ``max_model_len``(vLLM)
        또는 ``context_length`` — 양의 int 만. 실패·부재는 None."""
        try:
            r = requests.get(
                f"{self.base}/models", headers=self._auth_headers(), timeout=10
            )
            r.raise_for_status()
            for m in r.json().get("data", []):
                if m.get("id") == self.model:
                    for key in ("max_model_len", "context_length"):
                        ctx = m.get(key)
                        if isinstance(ctx, int) and ctx > 0:
                            return ctx
                    break
        except Exception:
            pass
        return None

    def _overflow_window(self) -> int | None:
        """의도적인 상한 초과 거절에서 실제 컨텍스트 창을 읽는다.

        메타데이터에 창을 싣지 않는 서버(omlx/mlx-lm)는 상한 초과 프롬프트를
        상한을 적은 400 으로 거절한다(예: ``"...exceeds max context window of
        262144 tokens"``). 런타임 복구층과 같은 ``parse_overflow_amounts`` 로
        그 숫자를 뽑는다.

        비용: 상한 초과 프롬프트는 *토크나이즈* 직후 거절된다 — eval·생성이
        없어 서버를 점유하지 않는다(omlx 실측 2026-05-30). 그래서 경계로
        좁혀 가는 이분 탐색은 하지 않는다: 상한 *이하* 프로브는 전체
        prompt-eval 을 일으켜 그동안 서버를 막는다.

        200(창이 프로브보다 큼)·상한 초과가 아닌 응답·숫자 없는 400·예외는
        None — 호출자가 기본값으로 떨어진다."""
        from agent_cli.context.overflow import (
            is_context_overflow,
            parse_overflow_amounts,
        )

        try:
            r = self._post(self._body("word " * _CONTEXT_PROBE_WORDS, max_tokens=16))
            if r.status_code == 200:
                return None
            text = r.text
            if not is_context_overflow(text):
                return None
            _actual, limit = parse_overflow_amounts(text)
        except Exception:
            return None
        return limit if (limit and limit > 0) else None

    def simple_chat(
        self, user_text: str, max_tokens: int, enable_thinking: bool = False
    ) -> str:
        r = self._post(
            self._body(user_text, max_tokens, enable_thinking=enable_thinking)
        )
        r.raise_for_status()
        data = r.json()
        # 사고를 별도 필드(vLLM reasoning 파서의 `reasoning_content`)나 블록
        # (Anthropic thinking)으로 주는 서버가 있다 — `<think>` 로 정규화해 태그
        # 검출과 하나로 합친다(필드/태그 어느 쪽이든 감지).
        thinking = self._thinking(data)
        content = self._text(data)
        return f"<think>{thinking}</think>{content}" if thinking else content

    def thinking_probe(self) -> bool:
        """2단계 사고 감지: ① 기본 프로브(모델이 기본으로 사고를 뱉으면 감지).
        미검출이면 ② 방언의 사고 켜기 스위치로 재프로브 — Qwen 처럼 사고가
        기본 OFF 인 모델을 잡는다. 재프로브 실패(서버가 스위치를 거부 등)는
        관용(→ 미지원); 실 Anthropic 모델은 레지스트리 기반이라 이 경로를 타지
        않는다. 1차 프로브 오류는 상위로 전파(검출 실패 = 기본값 폴백)."""
        if _detect_thinking(self.simple_chat("Say hello.", 512)):
            return True
        try:
            return _detect_thinking(
                self.simple_chat("Say hello.", 512, enable_thinking=True)
            )
        except Exception:
            return False

    def vision_probe(self) -> bool:
        """이미지 조각을 보내 두 색을 순서대로 맞히면 True."""
        from agent_cli import images

        mime, b64 = images.probe_image()
        content = [
            {"type": "text", "text": images.PROBE_QUESTION},
            self._image_part(mime, b64),
        ]
        r = self._post(self._body(content, max_tokens=64, probe=True))
        r.raise_for_status()
        return images.probe_answer_ok(self._text(r.json()))


class _OpenAITransport(_ProbeTransport):
    """OpenAI 호환 서버(omlx / vLLM / LM Studio / OpenAI) — ``/chat/completions``
    · ``Authorization: Bearer`` · ``choices[0].message``."""

    chat_path = "/chat/completions"

    def _auth_headers(self) -> dict:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    def _body(
        self,
        content,
        max_tokens: int,
        *,
        enable_thinking: bool = False,
        probe: bool = False,
    ) -> dict:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
        }
        # 기본 OFF 모델(Qwen 등)을 재프로브할 때 사고 스위치를 켠다.
        if enable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": True}
        # 판정 프로브는 결정적으로, 사고는 끈다(켜져 있으면 답이 사고 채널에
        # 갇혀 판정이 흐려진다).
        if probe:
            body["temperature"] = 0
            body["chat_template_kwargs"] = {"enable_thinking": False}
        return body

    @staticmethod
    def _message(data) -> dict:
        return data.get("choices", [{}])[0].get("message", {}) or {}

    def _text(self, data) -> str:
        return self._message(data).get("content") or ""

    def _thinking(self, data) -> str:
        msg = self._message(data)
        return msg.get("reasoning_content") or msg.get("reasoning") or ""

    def _image_part(self, mime: str, b64: str) -> dict:
        from agent_cli import images

        return images.openai_part(mime, b64)

    def grammar_probe(self) -> bool:
        """``guided_grammar`` 가 출력을 실제로 묶는가 — 한 단어짜리 문법을 보내
        정확히 그 단어만 돌아오면 True. 사고는 끈다(켜져 있으면 닫는 태그
        규칙이 없는 문법에 출력이 사고 채널로 갇혀 판정이 흐려진다)."""
        body = self._body("Say hello.", 16, probe=True)
        body["guided_grammar"] = f'root ::= "{_GRAMMAR_PROBE_ANSWER}"'
        r = self._post(body)
        r.raise_for_status()
        return self._text(r.json()).strip() == _GRAMMAR_PROBE_ANSWER


class _AnthropicTransport(_ProbeTransport):
    """Anthropic Messages API(실 Anthropic 또는 omlx 같은 호환 서버) —
    ``/messages`` · ``anthropic-version`` + ``x-api-key`` · ``content[]`` 블록.
    실 Anthropic 의 ``/v1/models`` 에는 창 메타가 없어 프로브·폴백·레지스트리로
    가고, omlx 류는 ``max_model_len`` 을 싣는다."""

    chat_path = "/messages"

    def _auth_headers(self) -> dict:
        h = {"anthropic-version": "2023-06-01"}
        if self.api_key:
            h["x-api-key"] = self.api_key
        return h

    def _body(
        self,
        content,
        max_tokens: int,
        *,
        enable_thinking: bool = False,
        probe: bool = False,
    ) -> dict:
        # probe 에 덧붙일 필드는 없다(통합 전 비전 프로브도 보내지 않았다).
        body: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        # 사고 켜기는 프로바이더가 실제로 보내는 모양과 같다
        # (``anthropic._THINKING_ON``, v10.72.0: 현행 Claude 는 ``budget_tokens``
        # 를 거절한다).
        if enable_thinking:
            from agent_cli.providers.anthropic import _THINKING_ON

            body["thinking"] = dict(_THINKING_ON)
        return body

    @staticmethod
    def _blocks(data) -> list:
        blocks = data.get("content") if isinstance(data, dict) else None
        return blocks if isinstance(blocks, list) else []

    def _text(self, data) -> str:
        """첫 **text 타입** 블록의 텍스트 — thinking 활성 응답은 blocks[0] 이
        thinking 블록이라 위치 고정 인덱싱은 빈 문자열을 돌려준다."""
        for b in self._blocks(data):
            if isinstance(b, dict) and b.get("type", "text") == "text":
                return b.get("text", "") or ""
        return ""

    def _thinking(self, data) -> str:
        return "".join(
            b.get("thinking", "") or ""
            for b in self._blocks(data)
            if isinstance(b, dict) and b.get("type") == "thinking"
        )

    def _image_part(self, mime: str, b64: str) -> dict:
        from agent_cli import images

        return images.anthropic_part(mime, b64)

    def grammar_probe(self) -> bool:
        """Anthropic Messages API 에는 디코딩 문법을 실을 필드가 없다."""
        return False
