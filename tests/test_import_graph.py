"""import 지도 가드 (docs/ARCHITECTURE.md §3) — AST 로만 본다.

1. 모듈 수준(top) import 그래프에 순환이 없다.
2. 순환을 피하려고 함수 안에 둔 import(대상이 top 간선만으로 출발 모듈에 닿는 lazy 간선)는
   아래 `CYCLE_AVOIDING_LAZY` 와 정확히 같다 — 늘어도 줄어도 실패한다.

정의는 `tests/import_graph.py`. 실제 콜드 스타트 순서는 `tests/test_import_cycles.py` 가 본다.
"""

from __future__ import annotations

from pathlib import Path

from tests.import_graph import build_graph

REPO = Path(__file__).resolve().parent.parent

# 순환 회피용 lazy 간선 — (출발 모듈, 대상 모듈). `agent_cli.` 접두는 뺐다.
CYCLE_AVOIDING_LAZY: set[tuple[str, str]] = {
    # 루프 엔진이 세션·구체 종류를 올려다본다 — 세션 자원(`browser_resource` 등)·깨우기 판정
    #    (`wake_sources`·`arrived_mail`·`harness_work_running`)·`release_owner`·`AgentRuntime`,
    #    main 레지스트리(`main_registry`)·`tool_agent`·`tool_delegate`, 스킬 실행기(`SkillLoop`).
    ("loop.core", "runtime"),
    ("loop.core", "subagent.agents_live"),
    ("loop.dispatch", "runtime"),
    ("loop.dispatch", "subagent.agents_live"),
    ("loop.kind", "runtime"),
    ("loop.kind", "subagent.agents_live"),
    ("loop.tool_bridge", "runtime"),
    ("loop.tool_bridge", "subagent.agents_live"),
    ("loop.tool_bridge", "subagent.oneshot"),
    ("loop.skill_invoke", "skills.executor"),
}


def _short(m: str) -> str:
    return m.removeprefix("agent_cli.")


def _graph():
    return build_graph(REPO, "agent_cli")


def test_no_module_level_import_cycle():
    cycles = _graph().top_cycles()
    assert not cycles, (
        "모듈 수준 import 순환 — 한쪽 방향을 고친다(아래층이 위층을 모듈 수준에서 import 하지 않는다). "
        "피할 수 없으면 함수 안 import 로 내리고 CYCLE_AVOIDING_LAZY 에 이유와 함께 올린다:\n"
        + "\n".join("  " + " ↔ ".join(_short(m) for m in c) for c in cycles)
    )


def test_cycle_avoiding_lazy_imports_are_the_named_list():
    found = {(_short(u), _short(v)) for u, v in _graph().cycle_avoiding_lazy()}
    added = sorted(found - CYCLE_AVOIDING_LAZY)
    gone = sorted(CYCLE_AVOIDING_LAZY - found)
    assert not added, (
        "새 순환 회피용 지연 import — 대상이 모듈 수준 import 만으로 출발 모듈에 닿는다. "
        "방향을 고치거나, 이유와 함께 tests/test_import_graph.py 의 CYCLE_AVOIDING_LAZY 에 올린다:\n"
        + "\n".join(f"  {u} -> {v}" for u, v in added)
    )
    assert not gone, (
        "더는 순환 회피용이 아닌 간선(지연 import 가 없어졌거나 대상이 더는 출발 모듈에 닿지 않는다) — "
        "CYCLE_AVOIDING_LAZY 에서 지운다:\n"
        + "\n".join(f"  {u} -> {v}" for u, v in gone)
    )


# ── 도우미 자체 ──────────────────────────────────────────────


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")


def test_helper_detects_module_level_cycle(tmp_path):
    _write(
        tmp_path,
        {
            "pk/__init__.py": "",
            "pk/a.py": "from pk import b\n",
            "pk/b.py": "try:\n    import pk.a\nexcept ImportError:\n    pass\n",
            "pk/c.py": "class K:\n    from pk.a import x\n",
        },
    )
    g = build_graph(tmp_path, "pk")
    assert g.top_cycles() == [["pk.a", "pk.b"]]
    assert g.top["pk.c"] == {"pk.a"}


def test_helper_skips_type_checking_block(tmp_path):
    _write(
        tmp_path,
        {
            "pk/__init__.py": "",
            "pk/a.py": "from pk import b\n",
            "pk/b.py": (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from pk import a\n"
                "else:\n"
                "    from pk import c\n"
            ),
            "pk/c.py": "",
        },
    )
    g = build_graph(tmp_path, "pk")
    assert g.top_cycles() == []
    assert g.top["pk.b"] == {"pk.c"}


def test_helper_tells_cycle_avoiding_lazy_from_others(tmp_path):
    _write(
        tmp_path,
        {
            "pk/__init__.py": "",
            "pk/low.py": "def f():\n    from pk import high\n    from pk import leaf\n",
            "pk/high.py": "from pk import low\n",
            "pk/leaf.py": "",
            "pk/both.py": "from pk import leaf\n\ndef g():\n    from pk import leaf\n",
        },
    )
    g = build_graph(tmp_path, "pk")
    assert g.lazy_edges() == {("pk.low", "pk.high"), ("pk.low", "pk.leaf")}
    assert g.cycle_avoiding_lazy() == {("pk.low", "pk.high")}
    assert g.top["pk.both"] == {"pk.leaf"} and g.lazy["pk.both"] == set()


def test_helper_parent_package_edges(tmp_path):
    _write(
        tmp_path,
        {
            "pk/__init__.py": "",
            "pk/sub/__init__.py": "",
            "pk/sub/deep.py": "from pk.sub import sibling\nfrom . import other\n",
            "pk/sub/sibling.py": "",
            "pk/sub/other.py": "",
            "pk/user.py": "import pk.sub.deep\nfrom pk.sub import NAME\n",
        },
    )
    g = build_graph(tmp_path, "pk")
    # 부모 패키지로 가는 간선은 넣고, 자기 조상 패키지로 가는 간선은 뺀다
    assert g.top["pk.user"] == {"pk.sub", "pk.sub.deep"}
    assert g.top["pk.sub.deep"] == {"pk.sub.sibling", "pk.sub.other"}
