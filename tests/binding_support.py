"""테스트용 정적 `ModelBinding` (v10.43.0) — 세션 코드는 부트가 만든 바인딩 하나를
참조하고, 테스트는 부품(provider·capabilities·model)으로 이 도우미를 부른다."""

from __future__ import annotations

from agent_cli.providers.binding import ModelBinding
from agent_cli.providers.credentials import Credentials


def make_binding(
    provider=None,
    capabilities=None,
    model: str = "m",
    provider_name: str = "openai",
    base_url: str = "",
    credentials=None,
    dialect=None,
) -> ModelBinding:
    if isinstance(dialect, str) or dialect is None:
        from agent_cli.dialects import get as get_dialect

        dialect = get_dialect(dialect) if dialect else get_dialect()
    return ModelBinding(
        provider=provider,
        provider_name=provider_name,
        base_url=base_url,
        credentials=credentials if credentials is not None else Credentials(),
        model=model,
        capabilities=capabilities,
        dialect=dialect,
    )
