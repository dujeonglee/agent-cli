"""등가성 코퍼스 빌더 (Phase 5 — docs/dialects/PHASE5.md §3.5·§7).

세 출처를 ``tests/equivalence/corpus/*.jsonl`` 로 고정한다 (한 줄 = ``{format,
text, source}``, (format, text) 중복 제거):

1. ``--histories DIR…``: 세션 디렉토리들의 ``history.jsonl`` assistant 레코드를
   **세션이 기록한 포맷**(``session.jsonl`` ``_meta.dialect``)으로 다시
   렌더한 캐노니컬 입력 — 라이브 모델이 실제로 낸 호출 모양.
2. ``--parse-inputs FILE``: ``AGENT_CLI_EQUIV_RECORD`` 로 스위트를 돌려 모은
   ``parse_turn`` 입력 — 손으로 만든 드리프트·변종·절단.
3. ``--emissions DIR…``: ``AGENT_CLI_RECORD_EMISSIONS=1`` 로 남긴 원문.

실행::

    AGENT_CLI_EQUIV_RECORD=/tmp/parse_inputs.jsonl pytest tests -q
    python tests/equivalence/build_corpus.py --parse-inputs /tmp/parse_inputs.jsonl \\
        --histories ../data/ws/*/.agent-cli/sessions/*
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
CORPUS = HERE / "corpus"
MD_ARRAY_MARKER = "## Action"  # 결정 1: md_array 헤더 관용은 버린다 — 코퍼스에서 제외


def _session_format(sdir: Path) -> str | None:
    try:
        first = (sdir / "session.jsonl").open(encoding="utf-8").readline()
        return json.loads(first).get("_meta", {}).get("dialect")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def from_histories(dirs: list[str]):
    from agent_cli import dialects as wf

    for pat in dirs:
        for d in glob.glob(pat):
            sdir = Path(d)
            fmt = _session_format(sdir)
            if not fmt:
                continue
            plugin = wf.get(fmt)
            for hist in [sdir / "history.jsonl", *sdir.glob("agents/*/history.jsonl")]:
                if not hist.is_file():
                    continue
                for line in hist.open(encoding="utf-8"):
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("role") != "assistant" or not rec.get("ops"):
                        continue
                    text = plugin.render_assistant_from_history(rec).get("content", "")
                    if text:
                        yield {"format": fmt, "text": text, "source": "history"}


def from_jsonl(path: str, source: str):
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("format") and isinstance(rec.get("text"), str):
                yield {"format": rec["format"], "text": rec["text"], "source": source}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--histories", nargs="*", default=[])
    ap.add_argument("--parse-inputs", default=None)
    ap.add_argument("--emissions", nargs="*", default=[])
    ap.add_argument("--out", default=str(CORPUS))
    a = ap.parse_args(argv)
    sys.path.insert(0, str(HERE.parent.parent))
    rows: dict[tuple[str, str], dict] = {}

    def add(it):
        for r in it:
            if MD_ARRAY_MARKER in r["text"] and r["format"] == "json_fc":
                continue  # 결정 1
            rows.setdefault((r["format"], r["text"]), r)

    add(from_histories(a.histories))
    if a.parse_inputs:
        add(from_jsonl(a.parse_inputs, "tests"))
    for pat in a.emissions:
        for d in glob.glob(pat):
            p = Path(d) / "emissions.jsonl"
            if p.is_file():
                add(from_jsonl(str(p), "emissions"))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    by_fmt: dict[str, list[dict]] = {}
    for r in rows.values():
        by_fmt.setdefault(r["format"], []).append(r)
    for fmt, items in by_fmt.items():
        items.sort(key=lambda r: (r["source"], r["text"]))
        with (out / f"{fmt}.jsonl").open("w", encoding="utf-8") as f:
            for r in items:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{fmt}: {len(items)} rows -> {out / (fmt + '.jsonl')}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("AGENT_CLI_WORKSPACE_CONFINE", "0")
    raise SystemExit(main())
