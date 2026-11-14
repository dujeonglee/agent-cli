"""모델 서버로 가는 요청의 인증 정보 하나 — `Credentials`.

종전엔 ``api_key: str`` 가 부팅 해석 → provider → 바인딩 → capability 프로브 →
``/models`` 조회로 흘렀고, 그 문자열을 헤더로 바꾸는 코드가 자리마다 따로였다
(provider 둘의 ``call``, 프로브 전송 둘, ``model_check``, setup 마법사). 이제 값은
이 객체 하나로 흐르고, 요청 헤더는 :meth:`Credentials.headers` 한 곳에서 만든다.

``Content-Type`` 은 POST 하는 쪽이 붙인다 — ``headers()`` 는 인증 헤더만 돌려줘
GET ``/models`` 와 POST 가 같이 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Anthropic Messages API 버전 헤더 값 — 본 호출·프로브·``/models`` 가 같이 쓴다.
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True, repr=False)
class Credentials:
    """API 키. 값은 ``repr``·``str`` 에 나오지 않는다(오류·로그로 새지 않게)."""

    api_key: str = ""

    def headers(self, provider: str) -> dict[str, str]:
        """인증 헤더 — 요청 헤더를 만드는 **유일한** 곳.

        - ``anthropic``: ``anthropic-version`` + 키가 있으면 ``x-api-key``.
        - 그 밖(OpenAI 호환): 키가 있으면 ``Authorization: Bearer``.
        """
        h: dict[str, str] = {}
        if provider == "anthropic":
            h["anthropic-version"] = ANTHROPIC_VERSION
            if self.api_key:
                h["x-api-key"] = self.api_key
        elif self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def __repr__(self) -> str:
        return f"Credentials(api_key={'***' if self.api_key else ''})"

    __str__ = __repr__
