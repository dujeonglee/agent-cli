"""web 표면은 `/api/knobs` 하나다 (2단계, v10.43.0 — docs/knobs/DESIGN.md §5.1).

종전 노브별 엔드포인트 일곱(`/api/thinking`·`/api/grammar`·`/api/max-agents`·
`/api/compaction`·`/api/stream-idle`·`/api/confirm-mode`·`/api/auditor`)과
sticky 일곱을 `GET /api/knobs` + `POST /api/knobs/{name}` + sticky ``knob:<name>``
으로 대체했다. 여기서 고정하는 것: 표의 모든 노브가 GET 에 나오고 POST 로 바뀐다
(표에서 돈다), 소유자 없음·잘못된 값·거부는 아무것도 바꾸지 않는다, 잠김 사유는
서버가 준다, 범위 보정(clamp)·무제한(0)·무진전 0=끔 같은 소유자 의미는 그대로다,
그리고 프런트는 노브별 fetch 없이 `KNOBS` 클라이언트 하나를 쓴다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_cli.context.manager import ContextManager
from agent_cli.knobs import KNOBS
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.render.web import WebConnection, WebRenderer
from agent_cli.subagent.agents_live import AgentRegistry
from agent_cli.web.server import WebServer, create_app
from tests.binding_support import make_binding

TOKEN = "t"


def _full(tmp_path, *, supports_thinking=True, supports_grammar=True):
    """소유자 넷이 전부 선 서버 — ctx·registry·renderer·auditor·binding."""
    from types import SimpleNamespace

    renderer = WebRenderer(session_dir=str(tmp_path))
    ctx = ContextManager(tmp_path / "s", max_context_tokens=10_000)
    caps = ModelCapabilities(
        32768, 4096, supports_thinking, supports_grammar=supports_grammar
    )
    server = WebServer(
        renderer,
        token=TOKEN,
        ctx=ctx,
        binding=make_binding(capabilities=caps, model="m1"),
    )
    server.agent_registry = AgentRegistry(tmp_path)
    server.auditor = SimpleNamespace(enabled=False, in_flight=0)
    return server, renderer, TestClient(create_app(server))


def _get(client, name):
    ks = client.get(f"/api/knobs?token={TOKEN}").json()["knobs"]
    return next(k for k in ks if k["name"] == name)


def _post(client, name, value):
    return client.post(f"/api/knobs/{name}?token={TOKEN}", json={"value": value}).json()


class TestTableDriven:
    def test_get_lists_every_knob_in_table_order(self, tmp_path):
        _s, _r, client = _full(tmp_path)
        ks = client.get(f"/api/knobs?token={TOKEN}").json()["knobs"]
        assert [k["name"] for k in ks] == [k.name for k in KNOBS]
        for k in ks:
            assert k["available"] is True and "value" in k and "locked" in k
            assert {
                "label",
                "help",
                "flag",
                "env",
                "kind",
                "choices",
                "default",
            } <= set(k)

    @pytest.mark.parametrize(
        "name,value,expect",
        [
            ("thinking", "on", "on"),
            ("reasoning_effort", "high", "high"),
            ("grammar", "off", "off"),
            ("max_agents", 3, 3),
            ("compaction_ratio", 0.8, 0.8),
            ("stall", "5m", 300),
            ("stall_attempts", 6, 6),
            ("auto_approve", True, True),
            ("auditor", "on", True),
            ("model", "m1", "m1"),  # 같은 이름 = no-op
        ],
    )
    def test_post_sets_and_broadcasts_every_knob(self, tmp_path, name, value, expect):
        _s, renderer, client = _full(tmp_path)
        r = _post(client, name, value)
        assert r["ok"] is True and r["value"] == expect, r
        assert _get(client, name)["value"] == expect
        assert renderer._sticky[f"knob:{name}"]["payload"]["value"] == expect
        # 재접속 snapshot 에 실린다 — 뷰어 동기화의 토대
        snap = renderer.register_connection(WebConnection(id="v2"))
        assert any(ev == "knob" and d.get("name") == name for ev, d in snap)

    def test_unknown_knob_is_404(self, tmp_path):
        _s, _r, client = _full(tmp_path)
        assert (
            client.post(f"/api/knobs/nope?token={TOKEN}", json={"value": 1}).status_code
            == 404
        )

    def test_bad_value_changes_nothing(self, tmp_path):
        _s, _r, client = _full(tmp_path)
        before = _get(client, "grammar")["value"]
        r = _post(client, "grammar", "maybe")
        assert r["ok"] is False and "grammar" in r["error"]
        assert _get(client, "grammar")["value"] == before

    def test_missing_owner_is_refused_not_crashed(self, tmp_path):
        renderer = WebRenderer()
        client = TestClient(create_app(WebServer(renderer, token=TOKEN)))
        assert _get(client, "max_agents")["available"] is False
        assert _get(client, "max_agents")["value"] is None
        r = _post(client, "max_agents", 3)
        assert r["ok"] is False and "no session owner" in r["error"]


class TestOwnerSemanticsSurvive:
    """종전 엔드포인트가 각자 보장하던 의미 — 범위 보정·무제한·끔 — 는 소유자의
    setter 에 있고 표를 거쳐도 같다."""

    def test_compaction_clamps_and_reports_range(self, tmp_path):
        server, _r, client = _full(tmp_path)
        st = _get(client, "compaction_ratio")
        assert (st["min"], st["max"], st["step"]) == (0.5, 1.0, 0.05)
        assert _post(client, "compaction_ratio", 0.1)["value"] == 0.5
        assert server.ctx.compaction_ratio == 0.5

    def test_max_agents_zero_is_unlimited(self, tmp_path):
        server, _r, client = _full(tmp_path)
        st = _get(client, "max_agents")
        assert st["min"] == 1 and st["unlimited"] is False
        r = _post(client, "max_agents", 0)
        assert r["value"] == 0 and r["unlimited"] is True
        assert server.agent_registry.max_agents == 0
        assert _post(client, "max_agents", -5)["value"] == 0  # 음수도 무제한

    def test_stall_two_axes_are_two_knobs(self, tmp_path):
        server, _r, client = _full(tmp_path)
        assert _post(client, "stall", 0)["value"] == 0  # 0 = 감지 끔
        assert server.ctx.stream_idle_timeout_s == 0
        assert _post(client, "stall", 30)["value"] == 60  # 하한 clamp
        assert _post(client, "stall_attempts", 99)["value"] == 10  # 상한 clamp
        assert server.ctx.stream_idle_timeout_s == 60  # 다른 축은 그대로

    def test_auto_approve_reaches_the_renderer(self, tmp_path):
        _s, renderer, client = _full(tmp_path)
        assert _post(client, "auto_approve", "on")["value"] is True
        assert renderer.auto_approve is True
        assert _post(client, "auto_approve", False)["value"] is False


class TestLockedReasonsComeFromTheServer:
    def test_thinking_locked_when_model_cannot_think(self, tmp_path):
        _s, _r, client = _full(tmp_path, supports_thinking=False)
        st = _get(client, "thinking")
        assert st["supports_thinking"] is False and "does not support" in st["locked"]
        _s2, _r2, client2 = _full(tmp_path / "b", supports_thinking=True)
        assert _get(client2, "thinking")["locked"] is None

    def test_grammar_locked_unless_support_is_recorded(self, tmp_path):
        _s, _r, client = _full(tmp_path, supports_grammar=None)
        st = _get(client, "grammar")
        assert st["supported"] is None and "not recorded" in st["locked"]
        _s2, _r2, client2 = _full(tmp_path / "b", supports_grammar=False)
        assert "cannot enforce" in _get(client2, "grammar")["locked"]
        _s3, _r3, client3 = _full(tmp_path / "c", supports_grammar=True)
        st = _get(client3, "grammar")
        assert st["locked"] is None and st["active"] is True
        assert _post(client3, "grammar", "off")["active"] is False
        # status.json 의 `grammar` 는 같은 sticky 에서 읽는다
        from agent_cli.web.instance_file import read_status_file

        assert read_status_file(tmp_path / "c")["grammar"] is False

    def test_model_state_carries_dialect_and_window(self, tmp_path):
        _s, _r, client = _full(tmp_path)
        st = _get(client, "model")
        assert st["value"] == "m1" and st["context_window"] == 32768
        assert st["dialect"] and st["locked"] is None


class TestChipShapeComesFromTheServer:
    """3단계(v10.45.0, §5.2): 상태 한 장에 칩 모양이 다 실린다 — 프런트는 그린다."""

    def test_every_state_carries_ui_badge_note_on_group(self, tmp_path):
        _s, _r, client = _full(tmp_path)
        for k in client.get(f"/api/knobs?token={TOKEN}").json()["knobs"]:
            assert {"ui", "badge", "note", "on", "group"} <= set(k), k["name"]
            assert {
                "icon",
                "group",
                "placement",
                "control",
                "scale",
                "unit",
                "choice_labels",
            } <= set(k["ui"])
            assert isinstance(k["badge"], str) and k["badge"], k["name"]  # 배지는 글자

    def test_group_members_share_one_badge(self, tmp_path):
        _s, _r, client = _full(tmp_path)
        st, at = _get(client, "stall"), _get(client, "stall_attempts")
        assert st["group"]["name"] == at["group"]["name"] == "stall"
        assert st["group"]["members"] == ["stall", "stall_attempts"]
        assert st["group"]["badge"] == at["group"]["badge"] == "10m×4"
        assert "at most 40 min" in st["group"]["note"]

    def test_setting_one_member_rebroadcasts_the_other(self, tmp_path):
        """묶음 배지는 두 멤버의 곱 — 한쪽을 바꾸면 다른 쪽 sticky 도 새 배지를 들어야
        재접속 snapshot 이 옛 배지를 재생하지 않는다."""
        _s, renderer, client = _full(tmp_path)
        client.post(f"/api/knobs/stall_attempts?token={TOKEN}", json={"value": 6})
        for name in ("stall", "stall_attempts"):
            assert (
                renderer._sticky[f"knob:{name}"]["payload"]["group"]["badge"] == "10m×6"
            )

    def test_model_switch_rebroadcasts_the_dependent_locks(self, tmp_path, monkeypatch):
        """모델이 바뀌면 사고·문법의 잠김이 바뀔 수 있다 — 전부 다시 싣는다."""
        import json

        import agent_cli.config as _config
        import agent_cli.dialects as _dialects

        target = tmp_path / "models.json"
        target.write_text(
            json.dumps(
                {
                    "models": {
                        "m1": {"context_window": 32768, "dialect": "json_fc"},
                        "m2": {
                            "context_window": 8000,
                            "dialect": "json_fc",
                            "supports_thinking": False,
                        },
                    }
                }
            )
        )
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
        monkeypatch.setattr(_config, "_cached_registry", None)
        monkeypatch.setattr(_dialects, "_override", None)
        _s, renderer, client = _full(tmp_path)
        assert _get(client, "model")["options"] == ["m1", "m2"]
        assert _get(client, "thinking")["locked"] is None
        r = client.post(f"/api/knobs/model?token={TOKEN}", json={"value": "m2"}).json()
        assert r["ok"] and r["badge"] == "m2"
        assert renderer._sticky["knob:thinking"]["payload"]["locked"]
        assert renderer._sticky["knob:reasoning_effort"]["payload"]["locked"]

    def test_stall_zero_locks_attempts(self, tmp_path):
        _s, _r, client = _full(tmp_path)
        client.post(f"/api/knobs/stall?token={TOKEN}", json={"value": 0})
        at = _get(client, "stall_attempts")
        assert at["locked"] and at["group"]["badge"] == "off"
        assert "is off" in at["group"]["note"]


class TestStatusFileMirrorsTheKnobs:
    """v10.46.0: ``status.json.knobs`` 는 노브 현재 값 전부 — 보드가 방의 값을
    거울처럼 기록해 다음 spawn 에 같은 플래그로 돌려준다 (docs/knobs/DESIGN.md §5.3)."""

    def _status(self, tmp_path):
        from agent_cli.web.instance_file import read_status_file

        return read_status_file(tmp_path)

    def test_boot_broadcast_writes_every_knob(self, tmp_path):
        from agent_cli.knobs import web_states

        server, renderer, _client = _full(tmp_path)
        renderer.broadcast_knobs(web_states(server.knob_session()))  # web 부트와 같다
        knobs = self._status(tmp_path)["knobs"]
        assert set(knobs) == {k.name for k in KNOBS}
        assert knobs["model"] == "m1" and knobs["thinking"] == "off"
        assert knobs["stall"] == 600 and knobs["auto_approve"] is False

    def test_each_set_rewrites_the_file(self, tmp_path, monkeypatch):
        import json

        import agent_cli.config as _config
        import agent_cli.dialects as _dialects

        target = tmp_path / "models.json"
        target.write_text(
            json.dumps(
                {
                    "models": {
                        "m1": {"context_window": 32768, "dialect": "json_fc"},
                        "m2": {"context_window": 8000, "dialect": "json_fc"},
                    }
                }
            )
        )
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
        monkeypatch.setattr(_config, "_cached_registry", None)
        monkeypatch.setattr(_dialects, "_override", None)
        _s, _r, client = _full(tmp_path)
        _post(client, "model", "m2")
        _post(client, "stall_attempts", 7)
        _post(client, "auditor", True)
        knobs = self._status(tmp_path)["knobs"]
        assert knobs["model"] == "m2" and knobs["stall_attempts"] == 7
        assert knobs["auditor"] is True

    def test_unavailable_knobs_are_left_out(self, tmp_path):
        """소유자 없는 노브(값 없음)는 쓰지 않는다 — 보드가 빈 값을 플래그로 넘기지 않게."""
        from agent_cli.knobs import web_states

        renderer = WebRenderer(session_dir=str(tmp_path))
        server = WebServer(renderer, token=TOKEN, ctx=ContextManager(tmp_path / "s"))
        renderer.broadcast_knobs(web_states(server.knob_session()))
        knobs = self._status(tmp_path)["knobs"]
        assert "stall" in knobs and "model" not in knobs and "max_agents" not in knobs


class TestFrontendIsOneRenderer:
    def test_chips_are_rendered_not_written(self, tmp_path):
        _s, _r, client = _full(tmp_path)
        js = client.get(f"/static/app.js?token={TOKEN}").text
        html = client.get(f"/?token={TOKEN}").text
        assert 'fetch("api/knobs")' in js and '"api/knobs/" + name' in js
        assert 'es.addEventListener("knob"' in js and "agentcli:knob" in js
        for old in (
            'fetch("api/thinking',
            'fetch("api/grammar',
            'fetch("api/max-agents',
            'fetch("api/compaction',
            'fetch("api/stream-idle',
            'fetch("api/confirm-mode',
            'fetch("api/auditor',
            "KNOBS.on(",  # 칩별 구독 — 렌더러 하나가 전부 듣는다
            "KNOBS.get(",
        ):
            assert old not in js, old
        assert js.count('document.addEventListener("agentcli:knob"') == 1
        assert 'id="knobs"' in html and 'id="knobs-header"' in html
        # 노브 이름·배지 글자가 프런트에 박혀 있지 않다 — 서버의 말을 보여줄 뿐
        for word in (
            "compaction_ratio",
            "max_agents",
            "reasoning_effort",
            "미지원",
            "감사 중",
        ):
            assert word not in js, word
