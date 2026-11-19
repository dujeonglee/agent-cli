"""메일함·요청 장부 오라클 — 지금 구현의 산출 == 고정 버전에서 만든 기대값(s01~s17 은 f44cf539, s21~s27 은
v10.144.0 — 1단계 기준 보강, s28~s31 은 v10.146.6 — 사람의 입력과 메일이 함께 main 앞에 설 때, s32·s33 은 v10.147.0 — 메일 런이 열리는
모양, s34·s35 는 vNEXT — 부모가 죽으면 그가 띄운 run 도 멈춘다, 닫힌 웹 펌프는 줄 선 말로 런을 열지 않는다).

기대값(``expected/*.json``)은 다시 만들지 않는다: 구조를 바꾸는 커밋에서 이 파일들이 바뀌면 오라클이 구현과 함께
움직인 것이다. ``SHA256SUMS`` 가 그 파일들을 고정하고, 드라이버의 import 목록이 "공개 표면만" 을 고정한다.
의도된 변경(시안 §0 ①②③)은 이 파일을 고치지 않고 바뀐 줄을 PR 에 적는다.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
from pathlib import Path

import pytest

from tests.oracle import driver

HERE = Path(__file__).parent
EXPECTED = HERE / "expected"

#: 드라이버가 import 해도 되는 것 — 공개 표면과 표준 라이브러리. 우편 구조(`agent_cli.post`)와 읽기 전용 조회가
#: 들어오면 안 된다. 사람의 입력 자리만 `MainInput`(v10.146.6 까지는 `InputQueue`)이다 — 그 앞의 고정 worktree 에서
#: 기대값을 다시 만들려면 그 트리의 드라이버를 쓴다.
ALLOWED_IMPORTS = {
    "__future__": {"annotations"},
    "argparse": None,
    "hashlib": None,
    "io": None,
    "json": None,
    "os": None,
    "re": None,
    "tempfile": None,
    "threading": None,
    "time": None,
    "pathlib": {"Path"},
    "types": {"SimpleNamespace"},
    "rich.console": {"Console"},
    "agent_cli.context.manager": {"ContextManager"},
    "agent_cli.main_input": {"MainInput"},
    "agent_cli.main": {"web_instance_is_active"},
    "agent_cli.providers.base": {"LLMResponse"},
    "agent_cli.providers.capabilities": {"ModelCapabilities"},
    "agent_cli.pump": {"ForeverPolicy", "QuietPolicy"},
    "agent_cli.render": {"get_renderer", "set_renderer"},
    "agent_cli.render.minimal": {"MinimalRenderer"},
    "agent_cli.runtime": {
        "SessionSpec",
        "build_session_owners",
        "build_session_stores",
        "teardown_session",
        "wire_session",
    },
    "tests.binding_support": {"make_binding"},
}

#: 드라이버가 부르면 안 되는 이름 — 레지스트리의 저장소와 1a 의 조회(f44cf539 에 없다)
FORBIDDEN_NAMES = (
    "_pending",
    "_mailboxes",
    "_reply_debts",
    "_sent",
    "_questions",
    "_parked_requests",
    "inbox_items",
    "in_flight",
    "held_items",
    "queued_for",
    "taken_by",
    "open_requests",
    "_push_reply",
    "_note_sent",
    "_agents",
)


def _sums() -> dict[str, str]:
    out = {}
    for line in (EXPECTED / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        out[name] = digest
    return out


class TestGuards:
    def test_driver_imports_only_the_public_surface(self):
        tree = ast.parse((HERE / "driver.py").read_text(encoding="utf-8"))
        seen: dict[str, set | None] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    seen.setdefault(a.name, None)
            elif isinstance(node, ast.ImportFrom):
                seen.setdefault(node.module, set()).update(a.name for a in node.names)
        assert set(seen) == set(ALLOWED_IMPORTS), set(seen) ^ set(ALLOWED_IMPORTS)
        for mod, names in seen.items():
            assert names == ALLOWED_IMPORTS[mod], (mod, names)
        assert not any(m.startswith("agent_cli.post") for m in seen)

    def test_driver_reads_no_store_and_no_new_query(self):
        tree = ast.parse((HERE / "driver.py").read_text(encoding="utf-8"))
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not used & set(FORBIDDEN_NAMES), used & set(FORBIDDEN_NAMES)

    def test_expected_files_match_their_sums(self):
        sums = _sums()
        assert sorted(sums) == sorted(f"{n}.json" for n in driver.SCENARIOS)
        for name, digest in sums.items():
            data = (EXPECTED / name).read_bytes()
            assert hashlib.sha256(data).hexdigest() == digest, name
            assert (EXPECTED / "raw" / name).is_file(), name


class TestNormalize:
    def test_ids_are_renumbered_per_prefix_in_order_of_first_appearance(self):
        text = "u-7 r-9 n-12 u-3 r-9 q-40 n-2 u-7"
        assert driver.renumber_ids(text) == "u-1 r-1 n-1 u-2 r-1 q-1 n-2 u-1"

    def test_a_shifted_numbering_normalizes_to_the_same_text(self):
        """깨우기 항목이 u- 를 먹지 않게 되어 번호가 밀려도 같은 판 — 할당 순서가 같다면."""
        old = "[u-1] wake [u-2] then r-3 re [u-2]"
        new = "[u-1] wake [u-5] then r-6 re [u-5]"
        assert driver.renumber_ids(old) == driver.renumber_ids(new)

    def test_ids_inside_words_are_left_alone(self):
        assert driver.renumber_ids("menu-3 run-2 u-3") == "menu-3 run-2 u-1"

    def test_paths_keys_and_times_are_removed(self):
        doc = {
            "b": {
                "ts": 1.5,
                "path": "/w/s/agents/agt-0000beef/x",
                "key": "agt-0000beef",
            },
            "a": {
                "key": "agt-1234abcd",
                "text": "took 3.2s, then 1m05s",
                "created_at": 9,
            },
        }
        got = driver.normalize(doc, root="/w", agents=["agt-1234abcd", "agt-0000beef"])
        assert got == {
            "a": {"key": "agt-A", "text": "took <d>, then <d>", "created_at": "<t>"},
            "b": {"ts": "<t>", "path": "<S>/s/agents/agt-B/x", "key": "agt-B"},
        }

    def test_a_key_right_after_a_serialized_newline_is_renamed(self):
        """died 통지는 줄 머리가 키다(``──\\nagt-…``) — 직렬화된 글에서 ``n`` 뒤도 키 경계다 (v10.144.1)."""
        doc = {"c": "── died [n-4] ──\nagt-1234abcd (bb): boom", "k": "agt-1234abcd"}
        got = driver.normalize(doc, root="/w", agents=["agt-1234abcd"])
        assert got == {"c": "── died [n-1] ──\nagt-A (bb): boom", "k": "agt-A"}

    def test_job_pids_are_removed(self):
        """꼬리의 작업 줄 pid 는 비결정이다 (v10.144.1) — 숫자 없는 안내문(``kill -- -<pid>``)은 그대로."""
        doc = {
            "t": "job #1 · pid 88283 · `sleep 30` · stop it: `kill -- -88283`; use `kill -- -<pid>`"
        }
        got = driver.normalize(doc, root="/w")
        assert got == {
            "t": "job #1 · pid <p> · `sleep 30` · stop it: `kill -- -<p>`; use `kill -- -<pid>`"
        }

    def test_key_order_does_not_follow_the_random_key(self):
        """키 이름을 바꾼 뒤 정렬한다 — 무작위 키의 사전순이 id 번호를 흔들지 않는다."""
        one = {"agt-ffffffff": {"id": "r-8"}, "agt-00000000": {"id": "r-9"}}
        two = {"agt-00000000": {"id": "r-8"}, "agt-ffffffff": {"id": "r-9"}}
        a = driver.normalize(one, root="/w", agents=["agt-ffffffff", "agt-00000000"])
        b = driver.normalize(two, root="/w", agents=["agt-00000000", "agt-ffffffff"])
        assert a == b


@pytest.fixture(scope="module")
def produced(tmp_path_factory):
    out = tmp_path_factory.mktemp("oracle-out")
    work = tmp_path_factory.mktemp("oracle-work")
    return out, driver.run_all(out, work=work)


@pytest.mark.parametrize("name", sorted(driver.SCENARIOS))
def test_current_implementation_matches_the_oracle(produced, name):
    out, sums = produced
    got = (out / f"{name}.json").read_text(encoding="utf-8")
    want = (EXPECTED / f"{name}.json").read_text(encoding="utf-8")
    if got != want:
        g, w = json.loads(got), json.loads(want)
        keys = [k for k in sorted(set(g) | set(w)) if g.get(k) != w.get(k)]

        # 어긋난 키 이름만으로는 CI 에서만 보이는 흔들림을 읽을 수 없다 — unified diff 앞 60줄. 글 속의
        # 줄바꿈(``\\n``)도 줄로 펴서 비교한다: 꼬리 한 통이 한 줄이라 펴지 않으면 그 한 줄 전체가 찍힌다.
        def lines_of(text: str) -> list[str]:
            return text.replace("\\n", "\n").splitlines()

        lines = list(
            difflib.unified_diff(
                lines_of(want), lines_of(got), "expected", "produced", lineterm=""
            )
        )[:60]
        pytest.fail(
            f"{name}: differs from the oracle in {keys}\n" + "\n".join(lines),
            pytrace=False,
        )
    assert sums[name] == _sums()[f"{name}.json"]
