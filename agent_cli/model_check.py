"""부팅 시 모델 이름 검증 — "없는 모델"과 "확인할 수 없음"을 가른다.

**왜 필요한가.** 종전엔 해석된 모델 이름을 아무도 확인하지 않았다. 서버에서
없어진 모델이 `config.json` 에 남아 있으면 그 이름이 **첫 LLM 호출까지
살아남아** 404 로 터졌고, 상주 에이전트에서는 그게 대화 한복판의 거부 카드로
보였다(v9.4.0 사용자 제보). 원인은 "기본값이 있다"가 아니라 **"그 이름을 아무도
확인하지 않는다"** 였다 — 손으로 친 `--model` 오타도 똑같이 뚫렸다.

**이미 신호는 있었다.** `providers.capabilities.resolve_capabilities` 는
`models.json` 에 없는 모델을 만나면 런타임 탐지로 `/v1/models` 를 호출하고,
실패하면 조용히 보수적 기본값으로 넘어가 **그대로 진행**했다. 즉 "이 모델은
서버에 없다"는 증거를 받아놓고 버리고 있었다. 이 모듈은 그 신호를 쓴다.

**핵심 구분 (설계 결정, docs/model-resolution)**: 조회 결과는 삼치(三値)다.

    목록을 받았고 이름이 있다    → 진행
    목록을 받았는데 이름이 없다  → **실패** (고칠 방법을 알려줄 수 있다)
    목록을 못 받았다             → **경고 후 진행**

마지막 줄이 중요하다. `_list_models` 는 실패를 전부 `[]` 로 뭉갰는데, 그러면
오프라인·`/models` 미지원 서버에서 "모델이 하나도 없다"로 읽혀 부팅이 막힌다.
확인할 수 없는 것을 틀렸다고 단정하면 그게 더 나쁜 회귀다.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass

__all__ = [
    "ModelListing",
    "ModelNotFound",
    "NoModelSelected",
    "list_models",
    "verify_model",
]

# 프로세스 수명 캐시 — 부팅 1회 + 에이전트 spawn 마다 조회하면 같은 목록을
# 반복해서 받는다. 키는 (base_url, provider): api_key 는 키에서 뺀다(비밀을
# 캐시 키로 쓰지 않는다; 한 프로세스가 같은 엔드포인트에 두 키를 쓰는 일은 없다).
_cache: dict[tuple[str, str], ModelListing] = {}


@dataclass(frozen=True)
class ModelListing:
    """`/v1/models` 조회 결과. ``available`` 이 False 면 ``models`` 는 무의미하다
    — "모델 0개"가 아니라 "묻지 못했다"이다."""

    models: tuple[str, ...]
    available: bool
    reason: str = ""

    def has(self, model: str) -> bool:
        return model in self.models

    def suggest(self, model: str) -> str:
        """오타로 보이는 근접 이름 하나 (없으면 "")."""
        if not model:
            return ""
        near = difflib.get_close_matches(model, self.models, n=1, cutoff=0.6)
        return near[0] if near else ""


class ModelNotFound(Exception):
    """해석된 모델이 서버 목록에 없다. 메시지는 사람이 읽고 바로 고칠 수 있게
    — 이름·서버·출처·사용 가능 목록을 담는다."""

    def __init__(self, model: str, listing: ModelListing, base_url: str, origin: str):
        self.model = model
        self.listing = listing
        self.base_url = base_url
        self.origin = origin
        super().__init__(model)


class NoModelSelected(Exception):
    """해석된 모델이 아예 없다. 종전엔 패키지가 ``gpt-4o`` 로 추측해 이 상태를
    "없는 모델 404" 로 바꿔 원인을 가렸다 — 이제 있는 그대로 말한다."""

    def __init__(self, listing: ModelListing, base_url: str):
        self.listing = listing
        self.base_url = base_url
        super().__init__("no model selected")


def list_models(
    base_url: str, api_key: str = "", provider: str = "openai"
) -> ModelListing:
    """`GET {base_url}/models` — 성공/실패를 구분해 돌려준다.

    `setup._list_models` 와 같은 엔드포인트지만 그쪽은 실패를 `[]` 로 뭉갠다
    (위저드는 수동 입력으로 떨어지면 그만이라 구분이 필요 없다). 여기서는
    구분이 계약이다."""
    key = (base_url, provider)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    result = _fetch(base_url, api_key, provider)
    _cache[key] = result
    return result


def _fetch(base_url: str, api_key: str, provider: str) -> ModelListing:
    import requests

    headers: dict[str, str] = {}
    if provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if api_key:
            headers["x-api-key"] = api_key
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = requests.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=10)
    except Exception as e:  # 네트워크·DNS·타임아웃 — 서버를 못 만났다
        # 이유는 **한 줄**이어야 한다. requests 의 예외 문자열은 urllib3 스택을
        # 통째로 실어 경고가 화면 절반을 먹었다(실장에서 확인). 사용자가 할 수
        # 있는 일은 어차피 "주소·기동 상태 확인" 하나라 분류면 충분하다.
        return ModelListing((), False, _short_reason(e))
    if r.status_code != 200:
        # 404 = `/models` 미지원 서버, 401/403 = 키 문제. 둘 다 "모델 없음"이
        # 아니라 "묻지 못했다"이다.
        return ModelListing((), False, f"HTTP {r.status_code}")
    try:
        data = r.json().get("data", [])
    except Exception as e:
        return ModelListing((), False, f"응답을 읽지 못함: {type(e).__name__}")
    ids = tuple(m["id"] for m in data if isinstance(m, dict) and m.get("id"))
    if not ids:
        # 200 인데 빈 목록: 형식이 다른 서버일 수 있어 단정하지 않는다.
        return ModelListing((), False, "목록이 비어 있음")
    return ModelListing(ids, True)


def _short_reason(exc: Exception) -> str:
    """예외를 한 줄 분류로. 원문은 길고 대부분 urllib3 내부 사정이다."""
    name = type(exc).__name__
    if "Timeout" in name:
        return "응답 없음(timeout)"
    if "ConnectionError" in name or "Connection" in name:
        return "연결할 수 없음"
    if "SSL" in name or "Certificate" in name:
        return "TLS 오류"
    text = str(exc).splitlines()[0] if str(exc) else ""
    return f"{name}: {text[:60]}" if text else name


def verify_model(
    model: str,
    base_url: str,
    api_key: str = "",
    provider: str = "openai",
    *,
    origin: str = "",
) -> ModelListing:
    """모델 이름을 검증한다. 통과하면 조회 결과를 돌려주고, 아니면 예외.

    - 이름 없음          → ``NoModelSelected``
    - 목록에 없음        → ``ModelNotFound``
    - 목록을 못 받음     → **통과** (호출부가 ``listing.available`` 로 경고)

    ``origin`` 은 그 이름이 어디서 왔는지(설정 파일 경로·``--model``·역할 md)
    — 실패 메시지에서 **고칠 곳을 지목**하는 데 쓴다.
    """
    listing = list_models(base_url, api_key, provider)
    if not model:
        raise NoModelSelected(listing, base_url)
    if not listing.available:
        return listing  # 확인 불가 ≠ 틀림
    if not listing.has(model):
        raise ModelNotFound(model, listing, base_url, origin)
    return listing


def is_interactive() -> bool:
    """대화형으로 모델을 고르게 해도 되는 상황인가.

    보드가 띄운 인스턴스·헤드리스 실행은 stdin 이 파이프라 물어보면 **멈춘다**
    (v9.1.0 에서 `agent-cli mcp` 가 파이프 뒤에서 걸린 것과 같은 함정).
    `AGENT_CLI_NO_INPUT` 은 테스트·자동화가 명시적으로 끄는 문."""
    import sys

    if os.environ.get("AGENT_CLI_NO_INPUT"):
        return False
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False
