"""도구 부수효과 인텐트 분류 (A3 계층 락의 전제, 병합 계획 M1).

계약 근거는 포크(Coagora) ``backend/src/agent/sandboxLock.ts:16-22`` 의
호환성 행렬과 ``turn.ts:109-117`` 의 ``lockScopeFor``. 여기서는 락 자체가
아니라 **분류가 맞는지**만 고정한다 — 락은 M4.
"""

import dataclasses
import json
from typing import ClassVar

import pytest

from agent_cli.tools import TOOLS
from agent_cli.tools.effect import EffectIntent, EffectKind


class TestDefaultIsExclusive:
    """자기 효과를 증명 못 하는 도구는 전부 UNKNOWN(=배타) — 안전측."""

    #: FILE_*/SHELL 로 좁혀진 도구들. 그 외는 전부 UNKNOWN 이어야 한다.
    NARROWED: ClassVar[set[str]] = {"read_file", "write_file", "edit_file", "shell"}

    def test_every_other_tool_is_unknown(self):
        for name, tool in TOOLS.items():
            if name in self.NARROWED:
                continue
            intent = tool.effect_intent({})
            assert intent.kind is EffectKind.UNKNOWN, name
            assert intent.is_exclusive, name

    def test_unknown_is_exclusive_even_with_a_path_argument(self):
        # 경로를 들고 있어도 분류가 UNKNOWN 이면 배타 — 경로 유무가 아니라
        # 종류가 판정을 지배한다(추측으로 좁히지 않는다).
        assert TOOLS["code_index"].effect_intent({"path": "a.py"}).is_exclusive


class TestFileTools:
    def test_read_file_is_scoped_read(self):
        intent = TOOLS["read_file"].effect_intent({"path": "src/a.py"})
        assert intent == EffectIntent(EffectKind.FILE_READ, "src/a.py")
        assert not intent.is_exclusive

    def test_write_file_is_scoped_write(self):
        intent = TOOLS["write_file"].effect_intent({"path": "src/a.py", "content": "x"})
        assert intent == EffectIntent(EffectKind.FILE_WRITE, "src/a.py")
        assert not intent.is_exclusive

    def test_edit_file_is_scoped_write(self):
        intent = TOOLS["edit_file"].effect_intent(
            {"path": "src/a.py", "op": "replace", "pos": "1#ab"}
        )
        assert intent == EffectIntent(EffectKind.FILE_WRITE, "src/a.py")

    def test_edit_file_line_delete_is_still_a_write(self):
        """``op="delete"`` 는 경로가 아니라 줄을 지운다 — FILE_DELETE 아님.

        FILE_DELETE 를 배타로 두는 근거(경로 소멸 → ENOENT 레이스)가
        성립하지 않으므로 같은 파일끼리만 직렬이면 충분하다.
        """
        intent = TOOLS["edit_file"].effect_intent(
            {"path": "src/a.py", "op": "delete", "pos": "1#ab"}
        )
        assert intent.kind is EffectKind.FILE_WRITE
        assert not intent.is_exclusive

    def test_wire_prefixed_keys_are_stripped(self):
        """``touched_paths`` 와 같은 규율 — override 는 표준 키를 읽는다."""
        intent = TOOLS["write_file"].effect_intent({"write_file_path": "src/a.py"})
        assert intent.path == "src/a.py"

    def test_missing_path_falls_back_to_exclusive(self):
        """빈 경로는 락 키로 신뢰할 수 없다 (``turn.ts:113-114``)."""
        for name in ("read_file", "write_file", "edit_file"):
            intent = TOOLS[name].effect_intent({})
            assert intent.path == ""
            assert intent.is_exclusive, name

    def test_non_string_path_does_not_crash(self):
        intent = TOOLS["write_file"].effect_intent({"path": 42})
        assert intent.path == ""
        assert intent.is_exclusive


class TestShell:
    def test_shell_is_always_exclusive(self):
        intent = TOOLS["shell"].effect_intent({"command": "cat a.py"})
        assert intent == EffectIntent(EffectKind.SHELL)
        assert intent.is_exclusive

    def test_shell_never_carries_a_path(self):
        """경로를 추측해 좁히지 않는다 — 파이프·변수전개·서브셸."""
        assert TOOLS["shell"].effect_intent({"command": "rm -r $DIR"}).path == ""


class TestCompatibilityMatrix:
    """``sandboxLock.ts:16-22`` 행렬을 술어 수준에서 고정."""

    def test_different_paths_are_both_parallelizable(self):
        a = EffectIntent(EffectKind.FILE_WRITE, "a.py")
        b = EffectIntent(EffectKind.FILE_READ, "b.py")
        assert not a.is_exclusive and not b.is_exclusive
        assert a.path != b.path  # 다른 키 → M4 락에서 병렬 진입

    def test_same_path_shares_one_key(self):
        a = EffectIntent(EffectKind.FILE_WRITE, "a.py")
        b = EffectIntent(EffectKind.FILE_READ, "a.py")
        assert a.path == b.path  # 같은 키 → M4 락에서 직렬

    def test_delete_and_package_are_exclusive(self):
        assert EffectIntent(EffectKind.FILE_DELETE, "src/").is_exclusive
        assert EffectIntent(EffectKind.PACKAGE).is_exclusive

    def test_whitespace_only_path_is_exclusive(self):
        assert EffectIntent(EffectKind.FILE_WRITE, "   ").is_exclusive

    def test_intent_is_frozen(self):
        intent = EffectIntent(EffectKind.SHELL)
        with pytest.raises(dataclasses.FrozenInstanceError):
            intent.kind = EffectKind.FILE_READ

    def test_kind_serializes_as_its_name(self):
        """``str`` 혼합 enum — 로그/JSON 에 값 그대로 나간다."""
        assert json.dumps(EffectKind.FILE_WRITE) == '"FILE_WRITE"'
