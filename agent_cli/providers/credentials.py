"""모델 서버로 가는 요청의 인증 정보 하나 — `Credentials`.

종전엔 ``api_key: str`` 가 부팅 해석 → provider → 바인딩 → capability 프로브 →
``/models`` 조회로 흘렀고, 그 문자열을 헤더로 바꾸는 코드가 자리마다 따로였다
(provider 둘의 ``call``, 프로브 전송 둘, ``model_check``, setup 마법사). 이제 값은
이 객체 하나로 흐르고, 요청 헤더는 :meth:`Credentials.headers` 한 곳에서 만든다.

``Content-Type`` 은 POST 하는 쪽이 붙인다 — ``headers()`` 는 인증 헤더(+추가 헤더)만
돌려줘 GET ``/models`` 와 POST 가 같이 쓴다.

추가 헤더 (config.json ``extra_headers``): 사용자가 적은 임의 헤더(게이트웨이의
``x-service-id`` 등)를 키가 가는 모든 요청에 같이 싣는다 — 같은 객체로 흐르니 노출
범위도 키와 같다. 검증 규칙은 :func:`validate_extra_headers` 하나이고 보드도 이것을
부른다. 오류 문구에는 헤더 **이름**만 쓰고 값은 쓰지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Anthropic Messages API 버전 헤더 값 — 본 호출·프로브·``/models`` 가 같이 쓴다.
ANTHROPIC_VERSION = "2023-06-01"

#: RFC 7230 ``token`` — 헤더 이름으로 쓸 수 있는 글자.
_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

#: 하니스가 소유하거나 HTTP 층이 정하는 헤더 — 추가 헤더로 덮어쓰지 못한다(소문자).
RESERVED_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "anthropic-version",
        "content-type",
        "content-length",
        "host",
        "transfer-encoding",
        "connection",
    }
)


class ExtraHeaderError(ValueError):
    """``extra_headers`` 설정이 규칙을 어겼다 — 문구에는 이름만 있다(값 없음)."""


def _label(name) -> str:
    """오류 문구에 쓸 이름 — 문자열이 아니거나 이상한 글자가 섞여도 한 줄로."""
    if not isinstance(name, str):
        return f"<{type(name).__name__}>"
    return repr(name[:60])


def validate_extra_headers(raw) -> list[dict]:
    """``extra_headers`` 원본을 검사해 정규형 목록으로 — 위반이면 :class:`ExtraHeaderError`.

    정규형: ``[{"name": str, "value": str, "enabled": bool}, ...]`` (순서 유지, 꺼진
    항목도 남긴다 — 저장하는 쪽(보드)이 그대로 되쓴다). ``None`` 은 빈 목록.

    규칙: 항목은 객체, ``name`` 은 RFC 7230 token, ``value`` 는 CR·LF·NUL 없는 문자열,
    ``enabled`` 는 없으면 true(있으면 bool), 하니스 소유 이름(:data:`RESERVED_HEADER_NAMES`)
    금지, 켜진 것끼리 이름 중복(대소문자 무시) 금지.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ExtraHeaderError("extra_headers must be a list of {name, value, enabled}")
    out: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ExtraHeaderError(f"extra_headers[{i}] must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ExtraHeaderError(f"extra_headers[{i}] needs a non-empty string name")
        if not _TOKEN_RE.match(name):
            raise ExtraHeaderError(
                f"extra header name {_label(name)} is not a valid HTTP header name"
            )
        if name.lower() in RESERVED_HEADER_NAMES:
            raise ExtraHeaderError(
                f"extra header {_label(name)} is set by agent-cli and cannot be overridden"
            )
        value = item.get("value", "")
        if not isinstance(value, str):
            raise ExtraHeaderError(
                f"extra header {_label(name)}: value must be a string"
            )
        if any(c in value for c in ("\r", "\n", "\0")):
            raise ExtraHeaderError(
                f"extra header {_label(name)}: value must not contain line breaks or NUL"
            )
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ExtraHeaderError(
                f"extra header {_label(name)}: enabled must be true or false"
            )
        if enabled:
            key = name.lower()
            if key in seen:
                raise ExtraHeaderError(
                    f"extra header {_label(name)} is enabled more than once"
                )
            seen.add(key)
        out.append({"name": name, "value": value, "enabled": enabled})
    return out


@dataclass(frozen=True, repr=False)
class Credentials:
    """API 키 + 추가 헤더. 값은 ``repr``·``str`` 에 나오지 않는다(오류·로그로 새지 않게)."""

    api_key: str = ""
    #: 켜진 추가 헤더만, 설정 순서대로 ``(name, value)``.
    extra_headers: tuple[tuple[str, str], ...] = ()

    def headers(self, provider: str) -> dict[str, str]:
        """인증 헤더 + 추가 헤더 — 요청 헤더를 만드는 **유일한** 곳.

        - ``anthropic``: ``anthropic-version`` + 키가 있으면 ``x-api-key``.
        - 그 밖(OpenAI 호환): 키가 있으면 ``Authorization: Bearer``.
        - 그 뒤에 추가 헤더(검증이 하니스 소유 이름을 막아 위를 덮지 않는다).
        """
        h: dict[str, str] = {}
        if provider == "anthropic":
            h["anthropic-version"] = ANTHROPIC_VERSION
            if self.api_key:
                h["x-api-key"] = self.api_key
        elif self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        for name, value in self.extra_headers:
            h[name] = value
        return h

    def __repr__(self) -> str:
        extras = ", ".join(f"{n}=***" for n, _ in self.extra_headers)
        return (
            f"Credentials(api_key={'***' if self.api_key else ''}, "
            f"extra_headers=[{extras}])"
        )

    __str__ = __repr__


def credentials_from_config(cfg: dict, *, api_key: str | None = None) -> Credentials:
    """config dict → `Credentials`. ``extra_headers`` 는 검증 뒤 켜진 것만.

    ``api_key`` 는 호출자가 해석한 값(플래그·환경변수 우선순위 — ``main._resolve_provider``)
    을 넘긴다. ``None`` 이면 ``cfg["api_key"]``. 검증 실패는 :class:`ExtraHeaderError`.
    """
    key = cfg.get("api_key", "") if api_key is None else api_key
    entries = validate_extra_headers(cfg.get("extra_headers"))
    return Credentials(
        api_key=key or "",
        extra_headers=tuple((e["name"], e["value"]) for e in entries if e["enabled"]),
    )
