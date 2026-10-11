# 워크스페이스 스냅샷 — 실수로 입력을 망쳐도 복구 가능하게

상태: 2026-10-11 **구현 완료 — PR ① v10.35.0(훅 배선), PR ② v10.36.0(저장소+내장 훅+CLI)**. 라이브 측정은 §4. 결정: 훅 배선은 네 루프 전부 · 기본 켬(내장 훅) ·
`.agent-cli/snapshot/` · GC 는 시작·마지막 매니페스트 유지 · `shutil` 만(외부 `cp` 없음 — OS
독립) · 상한 노브는 기본 **무제한**(장치만) · 최소 간격 10초 · 시작 스냅샷은 전체 복사.
(이후 엔진은 §3 대로 git 그림자 저장소로, 복구 표면은 네이티브 `snapshot` 도구로 확정 — 2026-10-11.)

## 1. 왜

벤치(tb21, Qwen3.6-35B)에서 입력 파괴로 진 과제가 둘이다 — `db-wal-recovery`(백업 없이
`sqlite3 main.db` 를 열어 WAL 소멸 → 세 버전 통틀어 0/5), `financial-document-processor`
(`rm -rf` 로 원본 문서 삭제). 모델은 사후엔 인과를 정확히 안다("I accidentally ran
PRAGMA wal_checkpoint which destroyed the WAL") — 행동 전에 떠올리지 못할 뿐이다.

"손대기 전에 백업하라" 는 **지시**는 이 모델급이 무시한다(v8.5x 실측: 시스템 프롬프트·
꼬리 규칙 0/5, 과제 본문 안에만 1/1). 그래서 **구조**로 푼다: 하니스가 런 시작 상태를
보관해 두고 "망치면 여기서 복구하라" 고만 알린다.

**실측 (2026-10-11, 어댑터가 `cp` 로 흉내 + 과제 꼬리 안내, db-wal × 3)**: 2/3 통과.
세 trial 모두 여전히 백업 없이 DB 를 먼저 열었고, 세 trial 모두 WAL 이 사라진 걸
알아챈 뒤 안내대로 `latest/` 에서 `cp` 로 복구했다("The WAL file has disappeared! Let
me check if it's in the snapshot"). 실패 1건은 복구 뒤 XOR 해독이 15분 안에 안 끝난 것.
→ 지시 0/5, 구조 3/3.

**실측 2 (진짜 훅 파일로, 0/3)**: 훅이 실행되지 않았다 — Python 훅(`HookRunner`)은
어느 루프에도 배선돼 있지 않다(`ports_for_main` 의 `hook_runner=None`, 사유 `_HOOKS_LATER`
"응용이 생기면 연결"). 그리고 과제 꼬리가 **무조건** "스냅샷이 있다" 고 약속해 모델이
없는 사본을 찾다 기준선보다 빨리 실패했다. → 안내는 **스냅샷이 성공한 뒤 훅이** 넣는다.

## 2. 목표 / 비목표

**목표**
- 의존성 0 (표준 라이브러리), 턴당 오버헤드 ≈ 0 (변화 없는 턴은 stat 한 바퀴).
- 런 시작 상태 + 변경 직전 복원 지점. 모델은 `cp` 로 복구한다(새 도구 없이).
- 코어 결합 0: 저장소 모듈 + **내장 훅**. 프로젝트 훅으로 끄거나 규칙을 덧붙일 수 있다.
- 틀려도 안전한 쪽으로: 큰 파일은 건너뛰되 기록, 실패하면 안내를 빼서 없는 사본을
  있다고 믿게 하지 않는다.

**비목표**
- 히스토리·diff 도구(git 대체) — 매니페스트 비교로 "바뀐 파일 목록" 정도만.
- 워크스페이스 밖(`/tmp`, `$HOME`) 보호.
- 에이전트용 작업 공간 분리(원본 격리) — 별도 opt-in 모드 후보, 여기 아님.

## 3. 구조 — git 그림자 저장소 (2026-10-11 결정)

저장 엔진은 **git** 이다(개발 환경에 git 이 없는 곳이 없다는 사용자 판단). 단 워크스페이스에
`.git` 을 만들지 않는다 — **그림자 저장소**: 저장소(`GIT_DIR`)는 `.agent-cli/snapshot.git`
(bare), 작업 트리(`GIT_WORK_TREE`)는 워크스페이스. 워크스페이스가 이미 저장소여도 그 index·
HEAD·히스토리는 건드리지 않는다(사용자 `git log` 에 "turn 12" 커밋이 섞이지 않는다).

```
.agent-cli/snapshot.git/     bare — objects · refs · index · info/exclude(우리 제외 목록)
<workspace>/                 작업 트리 — 아무것도 안 생김
git --git-dir=.agent-cli/snapshot.git --work-tree=. add -A --force && commit -m "turn 12"
git --git-dir=.agent-cli/snapshot.git --work-tree=. checkout HEAD~1 -- main.db-wal   # 복구
```

### 3.1 `agent_cli/snapshot.py` — `GitSnapshot` (subprocess git, 표준 라이브러리)

- `available()` — `shutil.which("git")`, 버전. 없으면 `enabled=False`.
- `init()` — `git init --bare`, `info/exclude` 에 제외 목록, 하위 저장소 스캔(§3.3).
- `checkpoint(label)` — `add -A --force` → 변경 있을 때만 `commit`. 최소 간격 10초.
  `--force` 와 `core.excludesFile` 무력화로 워크스페이스 `.gitignore` 를 무시한다(보호해야
  할 `*.db`·`data/` 가 거기 적혀 있기 일쑤).
- `changed(a, b)` — `diff --name-status`. `restore(path, at)` — **네이티브 `snapshot` 도구**가 부른다
  (`tools/snapshot_tool.py`, mode restore/list). 재측정에서 모델이 안내된 CLI(`agent-cli snapshot
  restore`)를 2/3 trial 에서 정확히 썼지만, 쉘로 우리 바이너리를 치는 건 PATH 에 기대고 도구와
  중복이라 도구로 확정하고 CLI 는 제거했다.
- **모든 호출의 env 고정**: `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1`(사용자 전역
  설정 차단 — gpgsign·hooksPath·autocrlf·excludesFile·safe.directory 를 한꺼번에 무력화),
  `GIT_AUTHOR_*`/`GIT_COMMITTER_*` = `agent-cli snapshot <snapshot@agent-cli>`(사용자 identity
  미사용 — 하니스 내부 커밋이다), `-c safe.directory=*`. git < 2.32 면 `-c` 개별 무력화.
- 범위: cwd 트리 전체. 제외: `.git`(모든 깊이 — 하위 저장소의 작업 파일은 포함), `.agent-cli`,
  `node_modules`, `__pycache__`, `.venv`. 상한 장치(파일 크기·파일 수)는 두되 기본 무제한.
- 세션당 인스턴스 하나(모든 루프 공유), 락으로 직렬화.

### 3.2 git 이 없을 때 / identity 가 없을 때

- git 없음: 훅이 자신을 끄고 **사람에게만** 한 줄(콘솔·웹 상태줄 `📸 스냅샷 꺼짐 — git 이
  없습니다`). 모델 안내 없음. 세션은 그대로. Python 복사 폴백 없음(엔진 두 벌 금지).
- identity: 사용자 설정을 보지 않는다 — env 고정(§3.1). 물을 UX 가 없다.

### 3.3 하위 git 저장소

git 은 작업 트리 안의 `.git` 디렉터리를 gitlink(커밋 sha)로만 기록한다. 그래서 시작 스캔에서
하위 저장소마다 `HEAD` 와 `status --porcelain`(수정·untracked)을 기록하고:
- 깨끗한 tracked 파일 → 그 저장소 자신이 복원(`git -C <sub> checkout -- <path>`)
- dirty·untracked 파일 → 그림자 저장소에 **직접** 담는다(`add --force` 로 경로 지정)
복구 안내는 매니페스트를 보고 경로별로 어느 쪽인지 말한다. 재귀는 없다 — 그림자는
워크스페이스당 하나, `.agent-cli` 는 제외라 자기 자신을 담지 않으며 bare 라 작업 트리가 없다.

### 3.4 훅 — 붙이는 자리

**Python 훅 배선(선결, PR ①)**: 세션당 `HookRunner` 하나를 `ports_for_main`·`ports_for_resident`·
`ports_for_skill`·`ports_for_oneshot` 의 `hook_runner` 에 꽂는다(서브에이전트 shell 도 파괴적).
`agent_cli/hooks/builtin/` 디렉터리를 로더가 프로젝트 훅보다 먼저 읽는다.

`agent_cli/hooks/builtin/10_snapshot.py`:
```python
EVENTS = ["OnSessionStart", "PreToolUse", "OnSessionEnd"]
def on_session_start(ctx):   # git 확인 → init → 시작 커밋 → 성공 시에만 안내 주입
def pre_tool_use(ctx):       # shell·edit_file·write_file 직전 checkpoint (최소 간격) — 함수명은 EVENT_TO_FUNC(on_ 없음)
def on_session_end(ctx):     # 마지막 checkpoint (세션 끝 상태도 복원 가능하게)
```

### 3.5 모델에게 보이는 것

시스템 섹션(시작 커밋 성공 시에만): 저장소 위치, "실행 전 상태는 직전 커밋", 복구 명령 한 줄
(`git --git-dir=.agent-cli/snapshot.git --work-tree=. checkout HEAD -- <path>`), 하위 저장소가
있으면 그 분기. 실패·끔은 사람에게만.

## 4. 작업 순서

1. **Python 훅 배선 (PR ①)** — `HookRunner` 세션 싱글턴 + 네 포트 빌더, 내장 훅 디렉터리
   로더. 동작 불변. 테스트: run/web/서브에이전트/스킬에서 `OnSessionStart`·`PreToolUse` 발화.
2. **`snapshot.py` + 내장 훅 (PR ②)** — 단위: 그림자 저장소가 워크스페이스에 흔적을 안 남김 /
   `.gitignore` 무시 / 변경 없으면 커밋 없음 / 삭제 뒤 이전 커밋에서 복원 / 하위 저장소
   dirty 파일 포함 / git 없음·identity 없음 처리 / 전역 설정 차단. 라이브: db-wal × 3
   (기준 2/3), financial × 2, 9과제 회귀.

## 5. 결정 (2026-10-11)

1. 훅 배선 범위: **네 루프 전부**, 세션당 `HookRunner` 하나.
2. **기본 켬**, 내장 훅으로. `AGENT_CLI_SNAPSHOT=0` 으로 끔.
3. 위치: **`.agent-cli/snapshot/`**.
4. 정리: **GC 만** (§3.3b).
5. **`shutil` 만** — 외부 `cp` 는 OS 의존이라 쓰지 않는다.
6. 상한: **장치만, 기본 무제한**. 최소 간격 10초. 시작 스냅샷은 전체 복사(shell 이 뭘
   만질지 모르므로 첫 shell 전에 전체 상태가 있어야 한다; 모델의 첫 shell 은 보통 1턴).
