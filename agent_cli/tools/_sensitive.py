"""민감 경로 사전 — 자격증명 읽기에 확인을 붙인다 (v9.9.2).

워크스페이스 봉쇄(`_confine`)는 *변경*을 막는다. 이 모듈은 다른 축을 본다:
**읽기**. 위협은 "혼란스럽거나 조종당한 에이전트가 자격증명을 흘리는 것"이고,
막는 수단은 확인 프롬프트다 — 여기도 샌드박스가 아니라 과속방지턱이다.

## 왜 별도 사전인가

봉쇄만으로는 두 방향 모두 틀린다. 워크스페이스 밖 읽기를 전부 물으면
커널/드라이버 작업이 헤더·툴체인을 수십 개 읽어 **프롬프트 폭풍**이 되고
(그래서 `read_file` 은 봉쇄 대상이 아니다), 전부 안 물으면 `~/.ssh/id_rsa`
가 무음으로 나간다. 사전은 그 사이를 가른다: **정확히 아는 것만** 묻는다.

## 목록의 원칙 — 정밀도 > 재현율

오탐 하나가 allow-all 피로를 학습시켜 게이트 전체를 무의미하게 만든다.
그래서 "그럴듯한 이름"을 넣지 않는다. 일부러 **뺀** 것들과 이유:

- ``.env`` — 에이전트는 보통 그 ``.env`` 를 가진 **프로젝트를 작업 중**이다.
  하루에 몇 번씩 물게 된다. 워크스페이스 경계가 그 파일의 정책이지 이 목록이
  아니다. ``.env*`` 는 ``.env.example`` 까지 삼킨다.
- ``*.pem`` — ``certifi/cacert.pem``, homebrew 의 ``cert.pem``, node_modules
  의 CA 번들·테스트 인증서가 지천이다. 진짜 신호는 확장자가 아니라
  내용(``-----BEGIN ... PRIVATE KEY-----``)이다.
- ``*.key`` — macOS 에선 Keynote 문서다. ``server.key``/``tls.key`` 픽스처도 흔하다.
- ``~/.zshrc``·``~/.bashrc`` — ``export ..._API_KEY=`` 가 실제로 들어 있는
  유출 벡터지만, "왜 PATH 에 없지"를 매일 읽는다. 아픈 컷이지만 의도적이다.
- ``~/.config/``·``~/.cargo/``·``~/Library/`` 같은 **디렉터리** — 툴체인과
  캐시라 상시 읽는다(``~/.cargo/registry/src`` 가 크레이트 소스를 읽는 길이다).
  안의 **정확한 자격증명 파일만** 집는다.

## macOS 우회 — 정규화가 사전만큼 중요하다

실측으로 확인한 셋(전부 이 기계에서 재현됨):

1. ``/etc`` → ``/private/etc`` 심볼릭. 사전 항목도 같이 정규화해야 한다.
2. ``/System/Volumes/Data/Users/<u>/.ssh`` 는 ``~/.ssh`` 와 **같은 파일**인데
   APFS firmlink 라 ``resolve()`` 가 접지 않는다 — 접두를 떼지 않으면 사전을
   그냥 지나간다.
3. 기본 APFS 는 대소문자 무시라 ``~/.SSH/id_rsa`` 가 열린다. ``realpath`` 는
   디스크상의 케이스가 아니라 **입력한 케이스**를 돌려준다.
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path

# ── 디렉터리 접두 (이 아래 전부) ──────────────────────────
_DIRS: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gcloud",
    "~/.azure",
    "~/.kube",
    "~/.password-store",
    "~/.config/op",  # 1Password CLI
    "~/.local/share/keyrings",  # GNOME keyring
    "~/Library/Keychains",  # macOS
    "/Library/Keychains",
)

# ── 정확한 파일 ────────────────────────────────────────────
# 상시 읽는 디렉터리 **안의** 자격증명만 집는다 — 디렉터리째 넣으면 툴체인
# 읽기가 전부 걸린다(`~/.cargo/` 는 크레이트 소스, `~/.docker/` 는 buildx).
_FILES: tuple[str, ...] = (
    "~/.agent-cli/config.json",  # 이 도구 자신의 api_key (config.py:3-5, :162)
    "~/.docker/config.json",
    "~/.netrc",
    "~/_netrc",
    "~/.git-credentials",
    "~/.config/git/credentials",
    "~/.npmrc",  # 홈만 — 프로젝트 `.npmrc` 는 레지스트리 설정이라 흔하다
    "~/.pypirc",
    "~/.config/gh/hosts.yml",  # `~/.config/gh/` 아님 — config.yml 은 환경설정
    "~/.cargo/credentials.toml",
    "~/.cargo/credentials",
    "~/.pgpass",
    "~/.my.cnf",
    "~/.terraform.d/credentials.tfrc.json",
    "~/.terraformrc",
    "~/.vault-token",
    "~/.env",
    # 셸·DB 히스토리 — 붙여넣은 토큰과 `PASSWORD '...'` 가 남는다
    "~/.bash_history",
    "~/.zsh_history",
    "~/.psql_history",
    "~/.mysql_history",
    # 같은 기계의 다른 AI 도구 토큰 — 같은 위협 종류. 여기서 멈춘다(롱테일 시작)
    "~/.claude/.credentials.json",
    "~/.codex/auth.json",
    "~/.config/github-copilot/hosts.json",
    # root 전용이라 값은 낮지만 오탐이 0 이라 공짜
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/master.passwd",
    "/etc/sudoers",
)

# ── 파일명 패턴 (디렉터리 무관) ────────────────────────────
# ``id_*`` 를 쓰지 않는 이유: ``id_rsa.pub`` 까지 잡는다(공개키는 비밀이 아니다).
_NAMES: tuple[str, ...] = (
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "privkey.pem",  # certbot 의 개인키 이름 — 공개 인증서는 이 이름을 안 쓴다
    "ssh_host_*_key",
    # 바이너리 키 컨테이너 — 텍스트로 읽어 쓸 데가 없어 오탐 비용이 사실상 0
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.keychain-db",
)

# 워크스페이스 안에서도 막는 파일명 — 이 도구 자신의 키라 위치와 무관하다.
_WORKSPACE_FILES: tuple[str, ...] = (".agent-cli/config.json",)

# APFS firmlink: ``/System/Volumes/Data/Users/x`` 와 ``/Users/x`` 는 같은 파일인데
# ``resolve()`` 가 접지 않는다. 떼지 않으면 사전을 그냥 지나간다(실측 확인).
_FIRMLINK_PREFIX = "/System/Volumes/Data"


def canonical(path: str | Path) -> Path:
    """비교 가능한 한 가지 모양으로 — ``~`` 확장 → firmlink 접두 제거 → resolve.

    대소문자는 여기서 접지 않는다(경로를 사람에게 보여줘야 하므로) — 비교 시점에
    :func:`_fold` 로 접는다.
    """
    p = Path(path).expanduser()
    s = str(p)
    if s.startswith(_FIRMLINK_PREFIX + "/"):
        p = Path(s[len(_FIRMLINK_PREFIX) :])
    try:
        return p.resolve()
    except (OSError, RuntimeError):  # 심볼릭 루프·권한 — 원형으로 비교
        return p


def _fold(s: str) -> str:
    """darwin 은 기본 APFS 가 대소문자 무시라 ``~/.SSH/id_rsa`` 가 열린다."""
    return s.casefold() if sys.platform == "darwin" else s


def _under(child: Path, parent: Path) -> bool:
    c, p = _fold(str(child)), _fold(str(parent))
    return c == p or c.startswith(p.rstrip("/") + "/")


def sensitive_reason(path: str | Path) -> str | None:
    """``path`` 가 민감하면 **사람이 읽을 이유**를, 아니면 ``None``.

    반환값이 그대로 확인 창에 들어가므로 "무엇에 걸렸는지"를 말해야 한다 —
    ``_confine`` 이 "워크스페이스 밖"이라고만 말해 진짜 이유를 못 밝히던 것과
    같은 실수를 반복하지 않는다.
    """
    if not path:
        return None
    resolved = canonical(path)

    for d in _DIRS:
        if _under(resolved, canonical(d)):
            return f"자격증명 디렉터리 {d}"
    for f in _FILES:
        if _fold(str(resolved)) == _fold(str(canonical(f))):
            return f"자격증명 파일 {f}"

    name = _fold(resolved.name)
    for pat in _NAMES:
        if fnmatch.fnmatch(name, _fold(pat)):
            return f"비밀키 파일명 규칙 `{pat}`"

    tail = _fold(str(resolved))
    for rel in _WORKSPACE_FILES:
        if tail.endswith("/" + _fold(rel)):
            return f"agent-cli 자신의 설정 `{rel}` (api_key)"
    return None
