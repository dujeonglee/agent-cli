"""`agent_cli/post/` 의 경계 — 잎이고, 아직 아무도 쓰지 않고, 머리만 읽는다 (AST 로만 본다).

- post 는 표준 라이브러리와 서로만 import 한다(`item_ids` 도 — id 는 호출자가 준다).
- agent_cli 의 다른 모듈은 post 를 import 하지 않는다. 3a 에서 풀 때는 이 가드를 지우지 않고 허용 목록에 한 줄을
  더한다.
- post 안에서 ``payload`` 의 내용을 읽지 않는다(통과·복사만), ``text`` 에 문자열 연산을 하지 않는다, 주소·id 를
  접두로 해석하지 않는다(같다/다르다로만 비교).
- 참조 모델은 post 를 import 하지 않는다.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POST = ROOT / "agent_cli" / "post"

#: post 를 import 해도 되는 agent_cli 모듈 — 3a 에서 여기에 한 줄씩 더한다
ALLOWED_USERS: frozenset[str] = frozenset()

#: 문자열로 해석하면 안 되는 머리 이름 — 주소·id·본문
OPAQUE = {
    "src",
    "dst",
    "id",
    "rid",
    "re",
    "text",
    "to",
    "debtor",
    "requester",
    "addr",
    "asker",
}
STR_METHODS = {n for n in dir(str) if not n.startswith("_")}


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def _post_files():
    return sorted(POST.glob("*.py"))


def test_post_imports_only_the_standard_library_and_itself():
    std = set(sys.stdlib_module_names) | {"__future__"}
    for path in _post_files():
        for mod in _imports(path):
            top = mod.split(".")[0]
            assert top in std or mod.startswith("agent_cli.post"), (path.name, mod)


def test_no_one_uses_post_yet():
    users = set()
    for path in (ROOT / "agent_cli").rglob("*.py"):
        if POST in path.parents:
            continue
        if any(
            m == "agent_cli.post" or m.startswith("agent_cli.post.")
            for m in _imports(path)
        ):
            users.add(str(path.relative_to(ROOT)))
    assert users == set(ALLOWED_USERS), users


def _attr_name(node) -> str:
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")


def test_post_never_opens_the_payload():
    """``payload`` 는 받거나 넘기거나 사본을 만들 뿐 — 첨자·``.get``·키 순회가 없다."""
    for path in _post_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Subscript):
                assert _attr_name(node.value) != "payload", (path.name, node.lineno)
            if isinstance(node, ast.Attribute) and isinstance(
                node.value, ast.Attribute
            ):
                assert node.value.attr != "payload", (path.name, node.lineno)
            if isinstance(node, ast.Compare) and any(
                _attr_name(c) == "payload" for c in node.comparators
            ):
                raise AssertionError(
                    (path.name, node.lineno, "membership test on payload")
                )


def test_post_does_no_string_work_on_addresses_ids_or_text():
    """주소·id·본문에 문자열 메서드(``startswith``·``split``…)·첨자·``in`` 이 없다 — 같다/다르다만."""
    for path in _post_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in STR_METHODS
                and _attr_name(node.func.value) in OPAQUE
            ):
                raise AssertionError((path.name, node.lineno, node.func.attr))
            if isinstance(node, ast.Subscript) and _attr_name(node.value) in OPAQUE:
                raise AssertionError((path.name, node.lineno, "subscript"))
            if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
            ):
                for side in (node.left, *node.comparators):
                    if _attr_name(side) in {"text"}:
                        raise AssertionError(
                            (path.name, node.lineno, "membership on text")
                        )


def test_the_reference_model_is_independent():
    ref = Path(__file__).with_name("reference.py")
    assert not any("agent_cli" in m for m in _imports(ref))


def test_the_guard_sees_a_violation(tmp_path):
    """가드 자신 — 접두 해석이 있으면 잡는다."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def f(env):\n    return env.src.startswith('user')\n", encoding="utf-8"
    )
    hits = [
        n
        for n in ast.walk(ast.parse(bad.read_text()))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in STR_METHODS
        and _attr_name(n.func.value) in OPAQUE
    ]
    assert len(hits) == 1
