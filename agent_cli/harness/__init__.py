"""하니스 작업 (v10.42.0, docs/auditor/DESIGN.md 4판).

하니스가 **자기 몫으로** 돌리는 일회성 서브에이전트 런과 그 위의 정책들.
팀원(`subagent.agents_live`)이 아니다 — 레지스트리·로스터·주소·`agents.json`·
생존 상한·보드 칩 어디에도 없다. 격리는 필터가 아니라 **부재**다.

- `jobs.py`   — `HarnessJobs`: 프로파일 로드(`agents/harness/`), 런 디렉토리
  (`<session>/harness/<job>/<label>/`), 스레드 실행, 결과를 main 메일함에 배달.
- `auditor.py` — `SessionAuditor`: main 의 사용자 런이 끝날 때마다 그 런의 history
  구간을 감사 작업으로 돌린다.
"""
