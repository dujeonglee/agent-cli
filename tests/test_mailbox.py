"""메일함 포트 `wake.Mailbox` (v10.133.27, g9) — 주소 하나에 묶인 레지스트리의 좁은 창.

레지스트리가 없으면 엔진이 하던 `reg is None` 분기와 같은 값을 돌려준다(그래서 포트 값은 언제나
Mailbox 다). 있으면 그 주소로 레지스트리에 그대로 넘긴다.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_cli.loop.kind import PrivateLoop
from agent_cli.runtime import MainLoop
from agent_cli.session_slots import set_agent_registry
from agent_cli.skills.executor import SkillLoop
from agent_cli.subagent.resident_loop import ResidentLoop
from agent_cli.wake import Mailbox

_S = object()


class TestWithoutRegistry:
    def test_values_match_the_engines_none_branches(self):
        mb = Mailbox(None, "run:x")
        assert mb.arrived() == 0
        assert mb.next_checkin_s() is None
        assert mb.parked_debtors() == []
        assert mb.wait(stop_event=None, still_waiting=lambda: True) == "nothing"
        mb.run_started()
        mb.arm_checkin()
        mb.drop()
        assert mb.has_inbox is False


class TestWithRegistry:
    def test_every_call_goes_to_the_registry_with_its_own_address(self):
        reg = MagicMock()
        reg.arrived_for.return_value = 2
        reg.next_checkin_s.return_value = 300
        reg.parked_debtors_for.return_value = ["agent:k"]
        reg.wait_mail.return_value = "mail"
        mb = Mailbox(reg, "agent:a")
        stop, still = object(), (lambda: True)
        assert mb.arrived() == 2
        assert mb.next_checkin_s() == 300
        assert mb.parked_debtors() == ["agent:k"]
        assert mb.wait(stop_event=stop, still_waiting=still) == "mail"
        mb.run_started()
        mb.arm_checkin()
        mb.drop()
        reg.arrived_for.assert_called_once_with("agent:a")
        reg.next_checkin_s.assert_called_once_with("agent:a")
        reg.parked_debtors_for.assert_called_once_with("agent:a")
        reg.wait_mail.assert_called_once_with(
            "agent:a", stop_event=stop, still_waiting=still
        )
        reg.run_started.assert_called_once_with("agent:a")
        reg.arm_checkin.assert_called_once_with("agent:a")
        reg.drop_mailbox.assert_called_once_with("agent:a")

    def test_take_is_the_given_inbox(self):
        mb = Mailbox(None, "agent:a", take=lambda: [{"record": 1}])
        assert mb.has_inbox is True
        assert mb.take() == [{"record": 1}]


class TestEveryKindHasOne:
    """다섯 종류의 포트는 모두 자기 주소의 Mailbox 를 갖는다 — 받은편지함은 상주·private 만."""

    def test_builders(self):
        reg = MagicMock()
        set_agent_registry(reg)
        main = MainLoop.make_ports(
            agent_registry=reg,
            mcp_manager=None,
            dequeue_user_message=None,
            enqueue_user_message=None,
            route_message=None,
        ).mailbox
        resident = ResidentLoop.make_ports(
            key="k1",
            message_handler=_S,
            questions=_S,
            absorb_inbox=list,
            peer_roster=_S,
        ).mailbox
        private = PrivateLoop.make_ports("run:t1").mailbox
        skill = SkillLoop.make_ports(agent_registry=None, owner="agent:p").mailbox
        got = {
            name: (type(mb).__name__, mb.owner, mb.has_inbox)
            for name, mb in {
                "main": main,
                "resident": resident,
                "private": private,
                "skill": skill,
            }.items()
        }
        assert got == {
            "main": ("Mailbox", "main", False),
            "resident": ("Mailbox", "agent:k1", True),
            "private": ("Mailbox", "run:t1", True),
            "skill": ("Mailbox", "agent:p", False),
        }
        # 레지스트리는 포트를 만들 때의 슬롯 것 — 종전 엔진이 읽던 것과 같다
        main.run_started()
        skill.run_started()
        assert [c.args for c in reg.run_started.call_args_list] == [
            ("main",),
            ("agent:p",),
        ]


class TestEngineDoesNotReadTheSlot:
    """엔진(`loop/`)은 우편 일을 `ports.mailbox` 로만 한다 — 전역 에이전트 레지스트리를
    import·참조하지 않는다(v10.133.28). 예외는 `kind.py`: 종류가 포트를 만드는 자리라 조립
    시점에 슬롯을 한 번 읽는다(`make_ports`)."""

    def test_engine_modules_never_name_get_agent_registry(self):
        import ast
        from pathlib import Path

        loop_dir = Path(__file__).resolve().parent.parent / "agent_cli" / "loop"
        offenders = []
        for path in sorted(loop_dir.glob("*.py")):
            if path.name == "kind.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.ImportFrom):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.Name):
                    names = [node.id]
                elif isinstance(node, ast.Attribute):
                    names = [node.attr]
                if "get_agent_registry" in names:
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], f"엔진이 전역 레지스트리를 읽는다: {offenders}"
