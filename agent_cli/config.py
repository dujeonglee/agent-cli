"""모델 서버 설정 — config.json 의 서버(endpoint) 표 + models.json 의 서버별 모델 표.

``~/.agent-cli/config.json``::

    {"endpoint": "home",                      # 선택된 서버 — 새 방과 CLI 의 기본값
     "endpoints": {"home": {"provider", "base_url", "api_key", "default_model",
                            "extra_headers"}, ...}}

``~/.agent-cli/models.json``::

    {"endpoints": {"home": {"models": {"<model id>": {...}}}, ...}}

- 한 프로세스(방 하나)는 서버 하나를 쓴다. 부트가 고른 이름을 :func:`use_endpoint` 로 한 번
  세우면, 모델 표 함수(:func:`get_model_entry`·:func:`save_model_entry` …)는 그 서버 칸을
  읽고 쓴다. 패키지의 ``default_models.json`` 은 서버와 무관한 바닥으로 늘 깔린다.
- 서버를 고르는 순서는 :func:`pick_endpoint_name` 하나다(``--endpoint`` > ``AGENT_CLI_ENDPOINT`` >
  세션 기록 > config 의 ``endpoint``). 없는 이름은 오류다 — 다른 서버로 넘어가지 않는다.
- **옛 모양(최상위에 ``endpoints`` 가 없는 파일)은 읽지 않는다** — "서버가 아직 없다" 다.
  셋업이 첫 서버를 저장하기 전에 :func:`retire_legacy_files` 가 이름만 ``.v1`` 로 바꿔 둔다.
- 서버가 하나도 골라지지 않은 실행(설정 파일 없는 컨테이너 등)은 env
  ``AGENT_CLI_PROVIDER``·``AGENT_CLI_BASE_URL``·``AGENT_CLI_API_KEY`` 가 이름 없는 서버가 된다
  (:func:`env_endpoint_config`). 서버가 골라지면 그 env 는 쓰지 않는다.

No project-local (``<cwd>/.agent-cli/``) config.json or models.json is read.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_cli.fsio import atomic_write_json
from agent_cli.paths import user_dir


@dataclass
class ProviderDefaults:
    base_url: str
    default_model: str


# 둘 다 **유저 스코프만** (docs/config-scopes): provider·키는 머신 사실이고, 모델 표는
# capability 탐지(콜드 로드 유발)의 캐시라 머신 단위가 맞다.
_CONFIG_PATH = user_dir() / "config.json"
_GLOBAL_MODELS_PATH = user_dir() / "models.json"
_PACKAGE_MODELS_PATH = Path(__file__).parent / "default_models.json"

#: 서버 이름 — 방이 이름으로 서버를 기억하므로 만든 뒤 바꾸지 않는다.
ENDPOINT_NAME_RE = re.compile(r"^[a-z0-9-]+$")

#: 서버가 골라지지 않은 실행의 이름 없는 서버 — env → 서버 값 키.
_ENV_FIELDS = {
    "AGENT_CLI_PROVIDER": "provider",
    "AGENT_CLI_BASE_URL": "base_url",
    "AGENT_CLI_API_KEY": "api_key",
}

# 문자열이 아닌 값을 그대로 두는 키 — 나머지는 ``str()`` 로 접는다.
# ``extra_headers`` 는 객체 목록이다(검증은 ``providers.credentials``).
_STRUCTURED_KEYS = frozenset({"extra_headers"})

# 프로바이더별 기본 **주소**만. 모델 이름은 여기 두지 않는다 (v9.5.0):
# 추측한 이름은 로컬 서버에서 절대 맞을 수 없고, "모델을 안 골랐다"를 "없는 모델
# 404"로 바꿔 원인을 가렸다 (`model_check.NoModelSelected`).
_PROVIDER_FALLBACK_URLS = {
    "anthropic": "https://api.anthropic.com/v1",
    "openai": "https://api.openai.com/v1",
}


class ConfigError(Exception):
    """설정 파일을 읽지 못했다 — 경로와 원문 오류(JSON 이면 줄·열)를 담는다 (v10.123.4).

    못 읽은 설정으로 계속 가면 기본값(공개 엔드포인트)이 된다 — 계속 가지 않는다."""

    def __init__(self, path: Path, detail: str):
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


class EndpointError(Exception):
    """고른 서버 이름을 쓸 수 없다 — 저장돼 있지 않거나 이름 규칙 위반. 문구는 사용자용(영어)."""


def read_config_file(path: Path) -> dict:
    """JSON 설정 파일 하나의 원본 dict — 못 읽거나 객체가 아니면 :class:`ConfigError`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ConfigError(path, str(e)) from e
    if not isinstance(data, dict):
        raise ConfigError(path, f"expected a JSON object, got {type(data).__name__}")
    return data


def is_legacy_shape(data: Mapping) -> bool:
    """옛 모양(v10.144.0 전)인가 — 내용이 있는데 최상위 ``endpoints`` 가 없다. 내용은 보지 않는다."""
    return bool(data) and "endpoints" not in data


def _user_config() -> dict:
    """유저 config.json 원본 — 없으면 {}."""
    return read_config_file(_CONFIG_PATH) if _CONFIG_PATH.is_file() else {}


def _endpoints_of(data: dict, path: Path) -> dict[str, dict]:
    if is_legacy_shape(data):
        return {}
    eps = data.get("endpoints", {})
    if not isinstance(eps, dict) or not all(isinstance(v, dict) for v in eps.values()):
        raise ConfigError(path, "'endpoints' must be an object of server objects")
    return eps


# ── 서버 표 (config.json) ────────────────────────────────────────


def config_path() -> Path:
    """유저 config.json 경로 (사용자 문구용)."""
    return _CONFIG_PATH


def list_endpoints() -> dict[str, dict]:
    """저장된 서버들 ``{이름: 원본 값}`` — 파일이 없거나 옛 모양이면 {}. 못 읽으면 ConfigError."""
    return _endpoints_of(_user_config(), _CONFIG_PATH)


def has_endpoints() -> bool:
    """서버가 하나라도 저장돼 있는가 (옛 모양·파일 없음은 False)."""
    return bool(list_endpoints())


def default_endpoint_name() -> str | None:
    """config 의 ``endpoint`` — 선택된 서버 이름(없으면 None). 옛 모양이면 None."""
    data = _user_config()
    if is_legacy_shape(data):
        return None
    name = data.get("endpoint")
    return name if isinstance(name, str) and name else None


def legacy_config_present() -> bool:
    """config.json·models.json 중 옛 모양 파일이 있는가 — 안내 문구용(내용은 읽지 않는다)."""
    for path in (_CONFIG_PATH, _GLOBAL_MODELS_PATH):
        if not path.is_file():
            continue
        try:
            if is_legacy_shape(read_config_file(path)):
                return True
        except ConfigError:
            continue
    return False


def pick_endpoint_name(
    flag: str | None,
    environ: Mapping[str, str],
    recorded: str | None,
    default: str | None,
) -> str | None:
    """서버를 고르는 순서 하나: ``--endpoint`` > ``AGENT_CLI_ENDPOINT`` > 세션 기록 > config 기본.

    이름을 고르기만 한다 — 그 이름이 저장돼 있는지는 :func:`endpoint_config` 가 본다."""
    for name in (flag, environ.get("AGENT_CLI_ENDPOINT"), recorded, default):
        if name and name.strip():
            return name.strip()
    return None


def endpoint_config(name: str) -> dict[str, Any]:
    """서버 하나의 값 — 빈 값은 빼고 문자열로 접는다(``extra_headers`` 는 목록 그대로).

    없는 이름은 :class:`EndpointError`, 못 읽은 파일은 :class:`ConfigError`."""
    endpoints = list_endpoints()
    if name not in endpoints:
        saved = ", ".join(sorted(endpoints)) or "none"
        raise EndpointError(
            f"Unknown endpoint {name!r} (saved servers: {saved}). "
            "Add it with 'agent-cli setup' or in the board admin."
        )
    return {
        k: (v if k in _STRUCTURED_KEYS else str(v))
        for k, v in endpoints[name].items()
        if v
    }


def env_endpoint_config(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """서버가 골라지지 않은 실행의 이름 없는 서버 — env 세 개의 값(빈 값은 뺀다)."""
    environ = os.environ if environ is None else environ
    return {key: environ[env] for env, key in _ENV_FIELDS.items() if environ.get(env)}


def endpoint_host(base_url: str) -> str:
    """주소에서 ``host[:port]`` 만 — 경로·쿼리·사용자 정보는 버린다(토큰을 쿼리나 userinfo 에
    싣는 게이트웨이가 있다). 목록·보드의 비관리 표면이 이것만 보인다."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base_url or "")
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if host and port else host


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not ENDPOINT_NAME_RE.match(name):
        raise EndpointError(
            f"Invalid endpoint name {name!r}: use lowercase letters, digits and '-'."
        )


def _write_endpoints(data: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_CONFIG_PATH, data, indent=2)  # 상태 파일 — fsio


def _new_shape_config() -> dict:
    """쓰기 전 읽기 — 옛 모양이면 거절한다(섞어 쓰지 않는다; 셋업이 먼저 .v1 로 옮긴다)."""
    data = _user_config()
    if is_legacy_shape(data):
        raise ConfigError(
            _CONFIG_PATH,
            "the file has the pre-10.144.0 shape; run 'agent-cli setup' to replace it",
        )
    _endpoints_of(data, _CONFIG_PATH)
    return data


def save_endpoint(name: str, fields: Mapping[str, Any]) -> None:
    """서버 하나를 만들거나 고친다 — ``fields`` 의 키만 덮고 그 서버의 다른 키·다른 서버는 그대로."""
    _check_name(name)
    data = _new_shape_config()
    entry = data.setdefault("endpoints", {}).setdefault(name, {})
    entry.update(fields)
    _write_endpoints(data)


def set_default_endpoint(name: str) -> None:
    """config 의 ``endpoint`` 를 저장된 서버 ``name`` 으로."""
    data = _new_shape_config()
    if name not in data.get("endpoints", {}):
        raise EndpointError(f"Unknown endpoint {name!r}.")
    data["endpoint"] = name
    _write_endpoints(data)


def set_endpoint_value(name: str, key: str, value: Any) -> bool:
    """저장된 서버 하나의 키 하나 — 실패(서버 없음·파일 문제)는 예외가 아니라 False.

    모델 검증이 대화형으로 고른 이름을 그 자리에서 저장하는 용도 — 저장을 못 해도
    이번 실행은 계속 굴러야 한다."""
    try:
        data = _new_shape_config()
        entry = data.get("endpoints", {}).get(name)
        if entry is None:
            return False
        entry[key] = value
        _write_endpoints(data)
        return True
    except (ConfigError, OSError):
        return False


def legacy_backup_path(path: Path) -> Path:
    """옛 파일을 남길 빈 이름 — ``<name>.v1``, 있으면 ``.v1-2``, ``.v1-3`` … (덮어쓰지 않는다)."""
    candidate = path.with_name(path.name + ".v1")
    n = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.v1-{n}")
        n += 1
    return candidate


def retire_legacy_files(paths: Iterable[Path] | None = None) -> list[Path]:
    """옛 모양(또는 읽지 못하는) 설정 파일을 읽지 않고 이름만 바꿔 남긴다 — 바꾼 새 경로들.

    셋업(CLI 마법사·보드 admin)이 첫 서버를 저장하기 **직전에만** 부른다. 보드는 자기
    경로를 넘긴다 — 두 표면이 같은 규칙(:func:`legacy_backup_path`)을 쓴다."""
    targets = list(paths) if paths is not None else [_CONFIG_PATH, _GLOBAL_MODELS_PATH]
    moved: list[Path] = []
    for path in targets:
        path = Path(path)
        if not path.is_file():
            continue
        try:
            keep = not is_legacy_shape(read_config_file(path))
        except ConfigError:
            keep = False  # 새 모양으로 읽을 수 없는 파일 — 덮어쓰기 전에 남긴다
        if keep:
            continue
        backup = legacy_backup_path(path)
        os.rename(path, backup)
        moved.append(backup)
    if moved:
        reload_registry()
    return moved


# ── 이 프로세스의 서버 ───────────────────────────────────────────

_UNSET = object()
_current: Any = _UNSET


def use_endpoint(name: str | None) -> None:
    """이 프로세스의 모델 표가 어느 서버 칸인지 세운다 — 부트가 **한 번** 부른다.

    ``None`` = 서버가 골라지지 않은 실행(패키지 바닥만 읽고 탐지 결과를 저장하지 않는다).
    이미 다른 값이 서 있으면 RuntimeError — 한 프로세스는 서버 하나다(조용히 바뀌지 않는다)."""
    global _current
    if _current is not _UNSET and _current != name:
        raise RuntimeError(
            f"this process already uses endpoint {_current!r}; cannot switch to {name!r}"
        )
    _current = name


def current_endpoint() -> str | None:
    """:func:`use_endpoint` 가 세운 이름 — 세우지 않았거나 None 이면 None."""
    return None if _current is _UNSET else _current


# ── 모델 표 (models.json, 서버별) ────────────────────────────────

_cached_registry: dict[str, Any] | None = None
# 캐시가 읽은 시점의 (서버, 파일 수정 시각) — 바뀌면 다음 조회가 다시 읽는다
# (v10.3.0: 보드 어드민이 바인딩을 고치면 재시작 없이 다음 spawn 부터 반영).
_cached_stamp: tuple | None = None


def _registry_stamp() -> tuple:
    return (current_endpoint(),) + tuple(
        p.stat().st_mtime_ns if p.is_file() else None
        for p in (_GLOBAL_MODELS_PATH, _PACKAGE_MODELS_PATH)
    )


def _read_json_quiet(path: Path) -> dict | None:
    """모델 표 파일 — 없으면 {}, 못 읽으면 경고 한 줄 뒤 None."""
    if not path.is_file():
        return {}
    try:
        return read_config_file(path)
    except ConfigError as e:
        print(f"[warn] Failed to load {e}", file=sys.stderr)
        return None


def _endpoint_models(data: dict, name: str | None) -> dict:
    """새 모양 models.json 에서 서버 ``name`` 의 모델 칸 — 없거나 옛 모양이면 {}."""
    if not name or is_legacy_shape(data):
        return {}
    section = data.get("endpoints", {})
    section = section.get(name) if isinstance(section, dict) else None
    models = section.get("models") if isinstance(section, dict) else None
    return models if isinstance(models, dict) else {}


def _load_registry() -> dict[str, Any]:
    global _cached_registry, _cached_stamp
    stamp = _registry_stamp()
    if _cached_registry is not None and stamp == _cached_stamp:
        return _cached_registry

    package = _read_json_quiet(_PACKAGE_MODELS_PATH) or {}
    merged: dict[str, Any] = {
        "models": dict(package.get("models", {})),
        "provider_defaults": dict(package.get("provider_defaults", {})),
    }
    user = _read_json_quiet(_GLOBAL_MODELS_PATH) or {}
    merged["models"].update(_endpoint_models(user, current_endpoint()))

    _cached_registry = merged
    _cached_stamp = stamp
    return _cached_registry


def reload_registry() -> None:
    """다음 조회가 models.json 을 다시 읽게 한다 (쓰기 직후)."""
    global _cached_registry
    _cached_registry = None


def models_path_label() -> str:
    """사용자 문구의 "어느 표" — ``~/.agent-cli/models.json (server 'home')``."""
    cur = current_endpoint()
    return (
        f"{_GLOBAL_MODELS_PATH} (server '{cur}')" if cur else str(_GLOBAL_MODELS_PATH)
    )


def get_model_entry(model: str) -> dict[str, Any] | None:
    """이 프로세스 서버의 모델 엔트리(패키지 바닥 포함), 없으면 None."""
    return _load_registry().get("models", {}).get(model)


def model_names() -> list[str]:
    """이 프로세스 서버에서 고를 수 있는 모델 이름들(패키지 바닥 포함), 이름순."""
    return sorted(_load_registry().get("models", {}))


def get_provider_defaults(provider: str) -> ProviderDefaults:
    """프로바이더의 기본 주소 — 패키지 표의 ``provider_defaults`` > 공개 엔드포인트."""
    entry = _load_registry().get("provider_defaults", {}).get(provider, {})
    fb_url = _PROVIDER_FALLBACK_URLS.get(provider, "http://127.0.0.1:8000/v1")
    return ProviderDefaults(
        base_url=entry.get("base_url", fb_url),
        # 빈 문자열 = "고른 적 없음". 추측하지 않는다.
        default_model=entry.get("default_model", ""),
    )


def _user_models_for_write() -> tuple[dict, dict] | None:
    """쓰기 준비 — ``(파일 전체, 이 서버의 models 칸)``. 쓸 수 없으면 None.

    서버가 골라지지 않았거나, 파일을 못 읽었거나, 옛 모양(섞어 쓰지 않는다)이면 None."""
    cur = current_endpoint()
    if not cur:
        return None
    data = _read_json_quiet(_GLOBAL_MODELS_PATH)
    if data is None:
        return None
    if is_legacy_shape(data):
        print(
            f"[warn] {_GLOBAL_MODELS_PATH} has the pre-10.144.0 shape and is not used; "
            "run 'agent-cli setup' to replace it",
            file=sys.stderr,
        )
        return None
    section = data.setdefault("endpoints", {}).setdefault(cur, {})
    return data, section.setdefault("models", {})


def _write_user_models(data: dict) -> bool:
    try:
        _GLOBAL_MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_GLOBAL_MODELS_PATH, data, indent=2)  # 상태 파일 — fsio
    except Exception as e:
        print(f"[warn] Cannot save to {_GLOBAL_MODELS_PATH}: {e}", file=sys.stderr)
        return False
    reload_registry()
    return True


def save_model_entry(model: str, entry: dict) -> bool:
    """런타임에 탐지한 모델을 이 서버의 칸에 저장한다.

    손으로 등록한 엔트리는 덮어쓰지 않는다(탐지 엔트리만 갱신). 탐지 엔트리를 갱신할 때도
    사용자가 손으로 넣은 키(예: dialect 바인딩)는 보존한다 (dialects §7-A1).
    Returns True if saved, False if already exists or cannot be written."""
    prepared = _user_models_for_write()
    if prepared is None:
        return False
    data, models = prepared
    existing_entry = models.get(model)
    if existing_entry is not None and not existing_entry.get("_auto_detected"):
        return False
    if existing_entry is not None:
        entry = {**existing_entry, **entry}
    models[model] = entry
    return _write_user_models(data)


def fill_model_entry(model: str, fields: dict) -> bool:
    """이 서버 칸의 엔트리에 **빠진 키만** 채운다 (v10.68.0) — 런타임 프로브 결과의 영속.

    아무 값도 덮어쓰지 않는다 — 이미 있는 키는 사용자 값이 이긴다. 엔트리가 없으면 아무것도
    하지 않는다(새 엔트리는 모델 감지의 몫). Returns True 면 썼다."""
    prepared = _user_models_for_write()
    if prepared is None:
        return False
    data, models = prepared
    entry = models.get(model)
    if not isinstance(entry, dict):
        return False
    missing = {k: v for k, v in fields.items() if k not in entry}
    if not missing:
        return False
    entry.update(missing)
    return _write_user_models(data)


def bind_user_dialect(model: str, name: str | None = None) -> bool:
    """이 서버 칸의 그 엔트리에 방언을 묶는다 (v10.127.0) — 묻기 전 판정과 쓰기를 한 곳에서.

    ``name`` 이 None 이면 **판정만**: 엔트리가 사용자 파일의 이 서버 칸에 있고 ``dialect`` 가
    빠졌거나 빈 값이면 True. 패키지 기본 표에만 있는 모델·값이 있는 ``dialect`` 는 False —
    답을 쓸 수 없는 질문을 하지 않게 묻기 **전에** 본다. ``name`` 을 주면 같은 판정 뒤 그 값을
    쓴다(성공 True). 값이 있는 ``dialect`` 는 덮어쓰지 않는다."""
    prepared = _user_models_for_write()
    if prepared is None:
        return False
    data, models = prepared
    entry = models.get(model)
    if not isinstance(entry, dict) or entry.get("dialect") not in (None, ""):
        return False
    if name is None:
        return True
    entry["dialect"] = name
    return _write_user_models(data)
