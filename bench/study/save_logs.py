#!/usr/bin/env python3
"""블록이 끝난 뒤 세션 로그 저장 — first-use study 진행자용.

에이전트 서버를 띄웠던 작업 폴더(리포 사본 폴더) 안의 최신 세션 로그를
`study-data/team<N>/<블록>/` 으로 복사한다. 세션 id 폴더를 직접 찾을
필요가 없도록 만든 도구다 (`22-study-run-kit.md` §4).

사용 (블록이 끝나 서버를 끈 직후, 저장소 최상위에서):

  .venv/bin/python bench/study/save_logs.py <작업폴더> <팀번호> <블록 A|B|C>

예:
  .venv/bin/python bench/study/save_logs.py ~/study/team1-pair1 1 A
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[3].upper() not in ("A", "B", "C"):
        sys.exit(__doc__)
    workspace = Path(sys.argv[1]).expanduser()
    team, block = sys.argv[2], sys.argv[3].upper()

    sessions_root = workspace / ".agent-cli" / "sessions"
    sessions = sorted(p for p in sessions_root.glob("*/") if p.is_dir())
    if not sessions:
        sys.exit(
            f"{sessions_root} 에 세션 폴더가 없다.\n"
            "- 작업 폴더 경로가 맞는지 (서버를 띄웠던 그 폴더인지) 확인\n"
            "- 서버를 --turn-metrics 로 띄웠는지 확인"
        )
    src = sessions[-1]  # 가장 최근 세션 = 방금 끝난 블록

    dst = Path("study-data") / f"team{team}" / block
    if dst.exists():
        sys.exit(
            f"{dst} 가 이미 있다 — 같은 블록을 두 번 저장하려는 게 아닌지 확인. 덮어쓰려면 먼저 그 폴더를 지울 것."
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)

    # 저장 확인: 인터뷰·채점이 기대는 두 파일이 실제로 들어왔는지.
    missing = [n for n in ("turns.jsonl", "history.jsonl") if not (dst / n).is_file()]
    if missing:
        sys.exit(
            f"복사는 됐지만 {', '.join(missing)} 이 없다 (원본: {src}).\n"
            "서버를 --turn-metrics 없이 띄웠거나, 다른 세션 폴더가 최신일 수 있다."
        )
    n_turn = sum(1 for _ in (dst / "turns.jsonl").open(encoding="utf-8"))
    n_hist = sum(1 for _ in (dst / "history.jsonl").open(encoding="utf-8"))
    print(f"저장 완료: {src}  →  {dst}")
    print(f"  turns.jsonl {n_turn}줄 / history.jsonl {n_hist}줄")
    print(f"확인용 다음 명령: .venv/bin/python bench/study/pick_events.py {dst}")


if __name__ == "__main__":
    main()
