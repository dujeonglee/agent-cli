"""`ModelBinding` — 세션이 지금 쓰는 모델 하나 (v10.43.0, docs/knobs/DESIGN.md §1.5).

종전엔 모델 이름·능력(`ModelCapabilities`)·방언·토큰 예산이 부팅 때 풀려
`SessionBootstrap` → run/web 지역변수 → `LoopConfig`(런마다) → `AgentRuntime`
(팀원·하니스 런) → `WebServer.runtime` 으로 **값으로 복사**됐다. 모델을 세션
도중 바꿀 길이 없었고, 같은 정보를 다섯 곳이 들었다.

이제 세션은 바인딩 **하나**를 들고 나머지는 참조만 한다. `LoopConfig.model`·
`.capabilities`·`.dialect` 는 바인딩을 읽는 속성이고, LLM 호출은 호출마다
`binding.model`·`binding.capabilities` 를 꺼낸다. 토큰 예산도 창 크기에서 매번
계산한다. 그래서 `switch()` 가 바꾼 모델은 **다음 LLM 호출**부터 실린다 — 캐시도,
무효화도 없다.

바꿀 수 있는 것은 모델 이름(과 그에 딸린 능력)뿐이다. 프로바이더 객체(엔드포인트·
키)와 방언은 세션 수명이다: history 렌더가 방언별이라 세션 도중 방언 전환은 막고,
`--dialect` 로 강제된 세션은 어떤 모델이든 된다. 모델은 models.json 에 등록된
것만 받는다 — 미등록 모델의 능력 프로브는 수십 초라 세션 도중 돌리지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from agent_cli.providers.capabilities import ModelCapabilities


@dataclass
class ModelBinding:
    """프로바이더(고정) + 모델·능력(바꿀 수 있음) + 방언(고정) + 예산 오버라이드."""

    provider: Any
    provider_name: str
    base_url: str
    api_key: str
    model: str
    capabilities: ModelCapabilities
    dialect: Any
    #: `--max-context-tokens` — 0 이면 창의 70% (`compute_token_budget`).
    context_budget_override: int = 0

    @property
    def max_context_tokens(self) -> int:
        """지금 모델의 창에서 매번 계산한다 — 모델이 바뀌면 예산도 따라간다."""
        if self.context_budget_override > 0:
            return self.context_budget_override
        from agent_cli.context.manager import compute_token_budget

        return compute_token_budget(self.capabilities.context_window)

    def switch(self, model: str) -> str:
        """모델을 바꾼다 — 오류 문자열 또는 ``""``. 같은 이름이면 no-op.

        조건: models.json 에 등록된 모델(능력을 프로브 없이 안다)이고, 그 모델의
        방언 바인딩이 지금 방언과 같다(또는 `--dialect` 강제). 성공하면 다음 LLM
        호출부터 새 모델·능력이 실린다."""
        model = (model or "").strip()
        if not model:
            return "model name is empty"
        if model == self.model:
            return ""
        from agent_cli.config import get_model_entry
        from agent_cli.dialects import resolve_dialect
        from agent_cli.providers.capabilities import _build_from_entry

        entry = get_model_entry(model)
        if entry is None:
            return (
                f"model '{model}' is not in models.json — register it (agent-cli "
                "setup / model detect) before switching to it mid-session"
            )
        try:
            dialect = resolve_dialect(model)
        except KeyError as exc:
            return str(exc.args[0] if exc.args else exc)
        if dialect.name != self.dialect.name:
            return (
                f"model '{model}' is bound to dialect '{dialect.name}' but this "
                f"session runs '{self.dialect.name}' — a session keeps one dialect "
                "(history is rendered per dialect); start a new session or force "
                "--dialect"
            )
        self.model = model
        self.capabilities = _build_from_entry(entry)
        return ""

    def pinned(self, model: str) -> ModelBinding:
        """프로파일·스킬이 고정한 다른 모델로 도는 런의 바인딩 — 같은 프로바이더,
        그 모델의 능력·방언(종전 `create_subagent_ctx` 의 `resolve_dialect(model)`
        규칙 그대로). 세션 바인딩과 무관한 복사본이라 `switch` 가 닿지 않는다."""
        model = (model or "").strip()
        if not model or model == self.model:
            return self
        from agent_cli.dialects import resolve_dialect
        from agent_cli.providers.capabilities import get_capabilities

        return replace(
            self,
            model=model,
            capabilities=get_capabilities(
                model, self.provider_name, self.base_url, self.api_key
            ),
            dialect=resolve_dialect(model),
        )
