"""방언이 안 묶인 모델은 터미널이면 그 자리에서 묻는다 (v10.127.0).

종전엔 탐지가 **성공**하면 `caps_to_entry` 결과가 방언 없이 저장되고 곧바로 ``No dialect for
model`` (rc=2) 였다. 방언을 묻는 곳은 탐지가 **실패**했을 때의 `_prompt_model_capabilities`
뿐이라, README 대로 설치한 사람의 첫 `run` 이 전부 여기서 멈췄다. "기본값·auto 없음,
사용자가 모델에 방언을 묶는다"(v10.3.0)는 그대로 — 묶을 기회를 주는 자리만 늘었다.
"""

from __future__ import annotations

import json

import pytest
import requests
import typer
from rich.console import Console

import agent_cli.config as _config
import agent_cli.dialects as _dialects
import agent_cli.main as m
from agent_cli import model_check
from agent_cli.providers import capabilities as _caps
from agent_cli.providers.capabilities import ModelCapabilities

URL = "http://127.0.0.1:1/v1"
MODEL = "fresh-model"
DETECTED = ModelCapabilities(
    context_window=65536,
    max_output_tokens=16384,
    supports_thinking=True,
    supports_grammar=False,
    supports_vision=False,
)


@pytest.fixture
def models_path(tmp_path, monkeypatch):
    target = tmp_path / "models.json"
    monkeypatch.setattr(_config, "_GLOBAL_MODELS_PATH", target)
    monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
    monkeypatch.setattr(_config, "_cached_registry", None)
    monkeypatch.setattr(_dialects, "_override", None)
    monkeypatch.setattr(_config, "_CONFIG_PATHS", [tmp_path / "config.json"])
    (tmp_path / "config.json").write_text('{"provider": "openai"}', encoding="utf-8")
    _config.reload_config()
    model_check._cache.clear()
    yield target
    model_check._cache.clear()
    _config.reload_config()
    _config.reload_registry()


@pytest.fixture
def server(monkeypatch):
    """가짜 `/v1/models`(그 모델이 있다) + 탐지 성공 더블."""

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": MODEL}]}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    probes = []

    def _detect(*a, **k):
        probes.append(a)
        return DETECTED

    monkeypatch.setattr(_caps, "_detect_runtime_capabilities", _detect)
    return probes


@pytest.fixture
def out(monkeypatch):
    con = Console(width=80, record=True, color_system=None)
    monkeypatch.setattr(m, "console", con)
    return con


class _Answers:
    def __init__(self, *answers):
        self._answers = list(answers)
        self.prompts = []

    def prompt_user(self, text, multiline=False):
        self.prompts.append(text)
        a = self._answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


def _terminal(monkeypatch, answers=None):
    monkeypatch.setattr("agent_cli.model_check.is_interactive", lambda: True)
    if answers is not None:
        monkeypatch.setattr(m, "get_renderer", lambda: answers)


def _entry(path):
    return json.loads(path.read_text(encoding="utf-8"))["models"][MODEL]


def _boot(quiet=False):
    return m._bootstrap_provider(
        "openai", MODEL, URL, "", None, 0, quiet=quiet, explicit_model=True
    )


def test_detected_entry_asks_saves_and_proceeds(models_path, server, out, monkeypatch):
    answers = _Answers("xml_fc")
    _terminal(monkeypatch, answers)
    binding = _boot()
    assert binding.dialect.name == "xml_fc"
    assert len(server) == 1
    entry = _entry(models_path)
    assert entry["dialect"] == "xml_fc"
    assert entry["context_window"] == 65536  # 탐지 결과는 그대로
    assert len(answers.prompts) == 1
    text = out.export_text()
    # 선택지마다 스펙의 한 줄 설명
    for name, summary in _dialects.summaries():
        assert name in text and summary in text
    # 다음 실행은 묻지 않는다
    answers2 = _Answers()
    _terminal(monkeypatch, answers2)
    model_check._cache.clear()
    assert _boot().dialect.name == "xml_fc"
    assert answers2.prompts == []


def test_unknown_answer_reprompts(models_path, server, out, monkeypatch):
    answers = _Answers("auto", "", "native_fc")
    _terminal(monkeypatch, answers)
    assert _boot().dialect.name == "native_fc"
    assert len(answers.prompts) == 3


@pytest.mark.parametrize("how", ["noninteractive", "quiet"])
def test_without_a_terminal_the_old_error_stays(
    models_path, server, out, monkeypatch, how
):
    answers = _Answers()
    if how == "noninteractive":
        monkeypatch.setattr("agent_cli.model_check.is_interactive", lambda: False)
        monkeypatch.setattr(m, "get_renderer", lambda: answers)
    else:
        _terminal(monkeypatch, answers)
    with pytest.raises(typer.Exit) as ei:
        _boot(quiet=how == "quiet")
    assert ei.value.exit_code == 2
    assert answers.prompts == []
    assert "dialect" not in _entry(models_path)  # 종전처럼 방언 없이 저장
    assert f"No dialect for model '{MODEL}'" in out.export_text()


def test_explicit_dialect_flag_never_asks(models_path, server, monkeypatch):
    answers = _Answers()
    _terminal(monkeypatch, answers)
    binding = m._bootstrap_provider(
        "openai", MODEL, URL, "", "json_fc", 0, explicit_model=True
    )
    assert binding.dialect.name == "json_fc"
    assert answers.prompts == []


def test_eof_is_the_old_error_and_ctrl_c_is_130(models_path, server, out, monkeypatch):
    _terminal(monkeypatch, _Answers(EOFError()))
    with pytest.raises(typer.Exit) as ei:
        _boot()
    assert ei.value.exit_code == 2
    assert "No dialect for model" in out.export_text()
    model_check._cache.clear()
    _terminal(monkeypatch, _Answers(KeyboardInterrupt()))
    with pytest.raises(typer.Exit) as ei:
        _boot()
    assert ei.value.exit_code == 130


def test_unsavable_entry_is_not_asked(models_path, out, monkeypatch):
    """엔트리가 사용자 파일에 없으면(여기선 탐지 결과 저장 실패) 답을 쓸 수 없다 — 묻지 않고
    종전 오류 2. 이번 런만의 바인딩도 만들지 않는다."""
    answers = _Answers("xml_fc")
    _terminal(monkeypatch, answers)
    monkeypatch.setattr(
        m,
        "_setup_provider",
        lambda *a, **k: (object(), DETECTED, MODEL, URL, "", "openai"),
    )
    with pytest.raises(typer.Exit) as ei:
        _boot()
    assert ei.value.exit_code == 2
    assert answers.prompts == []
    text = out.export_text()
    assert f"No dialect for model '{MODEL}'" in text
    assert "has no dialect bound" not in text
    assert _dialects._override is None


def _registered(path, dialect_value):
    entry = {"context_window": 32768, "supports_thinking": False}
    entry["dialect"] = dialect_value
    path.write_text(json.dumps({"models": {MODEL: entry}}), encoding="utf-8")
    _config.reload_registry()


@pytest.mark.parametrize("empty", ["", None], ids=["empty-string", "null"])
def test_empty_dialect_value_is_unbound_and_gets_filled(
    models_path, server, out, monkeypatch, empty
):
    _registered(models_path, empty)
    answers = _Answers("glm_argkey")
    _terminal(monkeypatch, answers)
    binding = _boot()
    assert binding.dialect.name == "glm_argkey"
    assert len(answers.prompts) == 1
    entry = _entry(models_path)
    assert entry["dialect"] == "glm_argkey"
    assert entry["context_window"] == 32768  # 다른 키는 그대로
    assert f"Saved dialect 'glm_argkey' to {models_path}" in out.export_text()


def test_bind_user_dialect_never_overwrites_a_value(models_path):
    _registered(models_path, "json_fc")
    assert _config.bind_user_dialect(MODEL) is False
    assert _config.bind_user_dialect(MODEL, "xml_fc") is False
    assert _entry(models_path)["dialect"] == "json_fc"
    assert _config.bind_user_dialect("not-there") is False


def test_write_failure_after_asking_keeps_the_message(
    models_path, server, out, monkeypatch
):
    _registered(models_path, "")
    answers = _Answers("xml_fc")
    _terminal(monkeypatch, answers)

    def _disk_full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(_config, "atomic_write_json", _disk_full)
    with pytest.raises(typer.Exit) as ei:
        _boot()
    assert ei.value.exit_code == 2
    assert len(answers.prompts) == 1
    text = " ".join(out.export_text().split())
    assert f"Could not save the dialect to {models_path}." in text
    assert "--dialect xml_fc" in text
    assert _entry(models_path)["dialect"] == ""
    assert _dialects._override is None


def test_long_models_path_is_one_chunk_at_80_columns(
    tmp_path, server, out, monkeypatch
):
    deep = tmp_path / ("d" * 90)
    deep.mkdir()
    target = deep / "models.json"
    monkeypatch.setattr(_config, "_GLOBAL_MODELS_PATH", target)
    monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
    monkeypatch.setattr(_config, "_cached_registry", None)
    monkeypatch.setattr(_dialects, "_override", None)
    monkeypatch.setattr(_config, "_CONFIG_PATHS", [tmp_path / "none.json"])
    _config.reload_config()
    model_check._cache.clear()
    _terminal(monkeypatch, _Answers("json_fc"))
    _boot()
    assert len(str(target)) > 80
    assert str(target) in out.export_text()
    _config.reload_registry()


# ── 설명의 원천은 스펙 하나 ─────────────────────────────────────


def test_every_registered_dialect_has_its_own_summary():
    pairs = _dialects.summaries()
    assert [n for n, _ in pairs] == _dialects.list_names()
    texts = [t for _, t in pairs]
    assert all(t.strip() for t in texts)
    assert len(set(texts)) == len(texts), (
        "native_fc 가 json_fc 의 것을 물려받으면 안 된다"
    )


@pytest.mark.parametrize("command", ["run", "web"])
def test_dialect_help_lists_every_registered_name(command):
    import typer.main

    # 클래스로 가르지 않는다 — typer 0.29 는 click 을 품어(`typer._click`) 옵션이
    # `click.Option` 의 인스턴스가 아니다. 옵션 이름으로 찾는다.
    cmd = typer.main.get_command(m.app).commands[command]
    opt = next(p for p in cmd.params if "--dialect" in getattr(p, "opts", ()))
    for name, summary in _dialects.summaries():
        assert name in opt.help
        assert summary in opt.help
