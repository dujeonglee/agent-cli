"""서버별 설정 파일(v10.144.0 모양)을 tmp 에 만들고 config 모듈이 그것을 보게 하는 테스트 도우미.

``config.json``·``models.json`` 의 경로는 모듈 상수라 monkeypatch 로 돌린다. 서버를 세우는
것(`config.use_endpoint`)은 프로세스에 한 번이라, conftest 의 autouse 픽스처가 테스트마다 비운다.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli import config


def point_config(
    monkeypatch,
    tmp_path: Path,
    *,
    endpoints: dict | None = None,
    default: str | None = None,
    models: dict | None = None,
    package: bool = True,
    use: str | None = None,
) -> tuple[Path, Path]:
    """``tmp_path`` 의 config.json·models.json 을 config 모듈의 경로로 — ``(config, models)``.

    ``endpoints`` 를 주면 새 모양 config.json 을 쓴다(``default`` 가 없으면 첫 이름).
    ``models`` 는 ``{서버 이름: {모델: 엔트리}}``. ``package=False`` 면 패키지 기본 표를 뺀다.
    ``use`` 를 주면 그 서버를 이 프로세스의 서버로 세운다."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.json"
    mdl = tmp_path / "models.json"
    monkeypatch.setattr(config, "_CONFIG_PATH", cfg)
    monkeypatch.setattr(config, "_GLOBAL_MODELS_PATH", mdl)
    if not package:
        monkeypatch.setattr(
            config, "_PACKAGE_MODELS_PATH", tmp_path / "no-package-models.json"
        )
    if endpoints is not None:
        chosen = default if default is not None else next(iter(endpoints), None)
        data: dict = {"endpoints": endpoints}
        if chosen:
            data["endpoint"] = chosen
        cfg.write_text(json.dumps(data), encoding="utf-8")
    if models is not None:
        mdl.write_text(
            json.dumps({"endpoints": {n: {"models": m} for n, m in models.items()}}),
            encoding="utf-8",
        )
    config.reload_registry()
    if use is not None:
        config.use_endpoint(use)
    return cfg, mdl


def point_models(monkeypatch, target: Path, endpoint: str = "t") -> None:
    """테스트가 쓴 새 모양 models.json(``{"endpoints": {endpoint: {"models": …}}}``) 하나만
    모델 표로 — 패키지 기본 표는 빼고, ``endpoint`` 를 이 프로세스의 서버로 세운다."""
    monkeypatch.setattr(config, "_GLOBAL_MODELS_PATH", target)
    monkeypatch.setattr(
        config, "_PACKAGE_MODELS_PATH", target.with_name("no-package-models.json")
    )
    config.reload_registry()
    config.use_endpoint(endpoint)
