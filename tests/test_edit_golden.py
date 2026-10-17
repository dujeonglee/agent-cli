"""edit_file 의미의 황금 말뭉치 — 편집 경로 변경의 회귀 가드.

무작위 편집 400건(단건 절반·같은 파일 묶음 절반, 오래된 ref·범위 밖·붙여 넣은
줄·형식 오류·잘못된 op 포함)의 결과를 얼려 두었다 (`tests/fixtures/edit_golden.json`).
성공 여부·결과 파일·모델이 읽는 문구·op 별 조각이 바이트 단위로 같아야 한다.

- v10.48.0: 단건 편집을 묶음 N=1 로 보내고 퍼지 매칭을 지우기 **전**의 구현
  (v10.47.1)으로 만들었다.
- v10.50.0: hash mismatch 가 재읽기 지시 대신 주변 ±3줄 창을 보여 준다. 다시
  만들 때 바뀐 사례 78건이 정확히 hash mismatch 사례 78건이고, 창을 걷어 내고
  재전송 문장을 되돌리면 옛 문구와 바이트 단위로 같음을 확인했다(성공·결과
  파일은 400건 모두 불변). 이후 편집 경로를 손대는 변경(턴
디스패치 통합, 에코 형식)이 의미를 조용히 바꾸면 여기서 깨진다 — 의도한 변경이면
말뭉치를 다시 만들고 차이를 PR 에 적는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.tools.edit_file import apply_edits_batch_parts, tool_edit_file

_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "edit_golden.json").read_text(
        encoding="utf-8"
    )
)


@pytest.fixture(autouse=True)
def _no_confine(monkeypatch):
    monkeypatch.setenv("AGENT_CLI_WORKSPACE_CONFINE", "0")


def test_corpus_covers_both_paths_and_failures():
    singles = [c for c in _CASES if c["parts"] is None]
    batches = [c for c in _CASES if c["parts"] is not None]
    assert len(singles) > 100 and len(batches) > 100
    assert any(c["ok"] for c in singles) and any(not c["ok"] for c in singles)
    assert any(c["ok"] for c in batches) and any(not c["ok"] for c in batches)


@pytest.mark.parametrize("i", range(len(_CASES)))
def test_matches_the_frozen_corpus(tmp_path, i):
    case = _CASES[i]
    f = tmp_path / "f.txt"
    f.write_text(case["text"], encoding="utf-8")
    ops = [{**o, "path": str(f)} for o in case["ops"]]
    if case["parts"] is None:
        res, parts = tool_edit_file(ops[0]), None
    else:
        res, parts = apply_edits_batch_parts(str(f), ops)
    rel = lambda s: s.replace(str(f), "<FILE>")
    assert res.success == case["ok"]
    assert f.read_text(encoding="utf-8") == case["after"]
    assert rel(res.output or res.error) == case["msg"]
    if parts is not None:
        assert [rel(p["content"]) for p in parts] == case["parts"]
