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
복구 안내는 매니페스트를 보고 경로별로 어느 쪽인지 말한다. (웹 Changes 패널이 하위 저장소 안을 세는 방식은 §8.)
하위 저장소의 경계는 디스크를 따른다(§9): 커밋 없는 저장소는 담지 않고(`add` 가 거부한 경로를 그 호출에서만 뺀다),
`.git` 을 지운 폴더는 안 파일을 일반 파일로 담고, 일반 파일로 담던 폴더가 커밋 있는 저장소가 되면 그 저장소 몫으로 넘긴다. 재귀는 없다 — 그림자는
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

## 6. 수리 — 벤치 실측 뒤 (v10.80.0, 2026-10-30)

보관된 harbor 런 전부(96 잡·716 trial)에서 `snapshot` 호출 166건을 셌다: restore 110(복구 48 ·
"다른 판 없음" 59 · pathspec 오류 3), list 56(그중 18건이 `path` 를 함께 보냈으나 버려짐).
검토 리포트: snapshot·history·memory 묶음(2026-10-30). 결함 여섯 중 다섯을 여기서 고치고
S5 는 사용자 결정으로 "유지 + 라벨" 이다.

| | 결함 | 수리 |
|---|---|---|
| S1 | **절대 경로가 늘 실패.** 모델은 과제문의 `/app/main.db-wal` 을 그대로 넣는다. `restore()` 가 후보를 고르는 `cat-file -e <commit>:<path>` 의 path 는 트리 기준 상대 경로라 절대 경로는 없는 객체 → 후보 전부 탈락 → "no snapshot holds a different version". 실패 59건의 88%(52건). `at` 을 준 호출만 `checkout at -- <abs>` 로 가서 통했고, 모델은 list→at 복사로 2~4턴을 우회했다(financial 한 trial 33회 호출). | `GitSnapshot.relative(path)` — 절대면 `resolve().relative_to(workspace)`(밖이면 분명한 오류), `./`·`..` 은 `normpath` 로 접음. restore·list 둘 다 이 경로를 쓴다. |
| S2 | `list` 가 `path` 를 버린다(18건). 모델은 "이 파일의 판들" 을 기대한다. | `log(path=)` = `git log -- <path>`; 머리줄 "Snapshots that changed <path>", 없으면 "No snapshot contains …". |
| S3 | 훅의 `MUTATING_TOOLS` 는 v10.66.0 부터 `python` 을 포함하지만 시스템 섹션·도구 설명은 셋만 말했다. | 상수를 `snapshot.py` 로 올리고(`MUTATING_TOOLS`·`MUTATING_TOOLS_PHRASE`) 훅과 두 문구가 같은 것을 본다. |
| S4 | 라벨 "before shell (turn 18)" 만으로는 어느 명령 직전인지 history 를 열어야 안다. | `checkpoint_label(tool, turn, summary)` — PreToolUse 의 `ctx.tool_input` 을 그 도구의 `summary_arg`(shell·python 전문, edit·write 경로)로 요약해 한 줄·60자로 접어 붙인다: `before shell (turn 7): sqlite3 main.db`. |
| S5 | 저장소가 세션을 넘는다 — `init()` 은 HEAD 가 있으면 재사용. list 에 이전 세션의 "turn 7" 이 섞인다. | **유지**(사용자 결정). 시작 커밋을 `--allow-empty` 로 **항상** 찍어 경계로 삼고 라벨에 세션 id: `session start (1761804554)`. 종전엔 워크스페이스가 그대로면 시작 커밋이 없어 경계도 없었다. 턴 체크포인트는 여전히 변경 없으면 커밋 없음. |
| S6 | 복구 결과 "restored main.db-wal from 610f3ac" 뿐 — 모델이 같은 명령을 또 돌려 또 지운다(financial 33회·db-wal 반복). | 결과에 원인의 자리: `— intact at "<멀쩡했던 마지막 스냅샷 라벨>", changed by "<달라진 채 찍힌 첫 스냅샷 라벨>".` 달라진 상태가 아직 어느 스냅샷에도 없으면 `— intact at "<가장 최근 스냅샷>", changed after that snapshot.` 라벨이 S4 의 명령 요약을 담으므로 이 한 줄이 "turn 7 의 sqlite3 가 지웠다" 를 말한다. `at` 을 준 복구는 그 라벨만. |

실패 문구 둘로 갈랐다: 어느 스냅샷에도 없는 경로는 `no snapshot contains 'app/main.db-wal' (paths are
relative to the working directory)`, 전부 지금과 같은 내용이면 `every snapshot of 'main.db' matches what is on
disk now — nothing to restore`. 종전 한 문구("no snapshot holds a different version")는 두 경우를 구별하지 못해
모델이 경로 오타를 "판이 없다" 로 읽었다.

**지표**(다음 벤치, GLM 2회): restore 의 "다른 판 없음" 비율(59/110 → 0 근방), 복구 뒤 같은 파괴 반복 수,
list→at 우회 시퀀스 수, db-wal·financial 통과(35B 가 민감). 테스트 `tests/test_snapshot.py`
(`TestSessionBoundary`·`TestLabel`·원인 줄·경로 정규화), `tests/test_snapshot_tool.py`.

## 7. 기준점 — 웹 Changes 패널 (v10.137.0, 2026-11-14)

사람이 보는 목록 "기준점 이후 디스크에서 바뀐 파일" 이다. 모델이 받는 것은 바뀌지 않는다.

- **기준점**: ref `refs/agent-cli/baseline` 이 있으면 그 커밋(사람이 Clear 를 누른 때). 없으면 **이 세션 id 로 찍힌
  가장 이른** `session start (<sid>)` 커밋 — resume 은 프로세스마다 시작 커밋을 새로 찍으므로 "가장 최근" 이면 resume
  할 때마다 목록이 비워진다. 그것도 없으면 HEAD. `GitSnapshot.baseline()` 이 `(commit, kind, when)` 을 준다.
- **지금 디스크**: 커밋을 만들지 않는다. 임시 색인(`GIT_INDEX_FILE`, 저장소 안 `index.changes-<pid>-<thread>`)에
  체크포인트와 같은 pathspec 으로 `add -A --force` → `write-tree`. 실제 색인을 **mtime 까지** 복사해 시작한다 —
  stat 캐시로 안 바뀐 파일을 다시 해시하지 않되, 새 mtime 의 복사본은 racy-git 검사를 꺼 같은 초·같은 크기 편집을
  놓친다(실측). 실제 색인·HEAD·`_last_checkpoint` 를 건드리지 않으므로 런 도중에 불러도 체크포인트와 엉키지 않고
  `_lock` 도 잡지 않는다.
- **목록·diff**: 기준 커밋 ↔ 그 트리의 `diff --no-renames -z`(`--name-status`·`--numstat`) — 이름 바뀜은 D+A, 바이너리는
  줄 수 없음. 파일 diff 는 `relative` 로 경로를 검사하고 `CHANGE_DIFF_MAX_LINES` 에서 자른다.
- **Clear**: 지금 트리를 `commit-tree`(부모 HEAD, 라벨 `baseline (cleared)`) 하고 이전 기준 커밋을
  `refs/agent-cli/baseline-prev` 에, 새 커밋을 `refs/agent-cli/baseline` 에 둔다. **HEAD 는 옮기지 않는다** — 모델의
  `snapshot` `list` 는 HEAD 로그라 사람의 커밋이 섞이지 않는다. ref 가 커밋을 붙잡아 gc 에 안전하다.
- **Undo**(한 단계): `baseline-prev` 가 Clear 커밋이면 그리로, 세션 시작(또는 HEAD)이면 `baseline` ref 를 지운다.
  `baseline-prev` 는 지운다 — 두 번째 Undo 는 무동작.
- 저장소가 세션을 넘어 이어지므로 Clear 한 기준점은 방을 다시 띄워도·`--resume` 해도 남는다. 스냅샷이 꺼지면
  (`AGENT_CLI_SNAPSHOT=0`·git 없음) 패널도 없다.
- **입구 셋**(v10.144.2): 헤더 📁 의 숫자 배지·main 의 마지막 `complete` 카드 안의 한 줄(둘 다 바뀐 것이 있을 때만),
  그리고 📁 Workspace files 서랍 머리 아래 **늘 서 있는 한 줄** — N(강조, `View changes ›`)·0(흐림, `Open ›`
  — 빈 패널)·꺼짐(점선, 누를 수 없음, 까닭 한 줄).
- **Clear 는 늘 눌린다**(v10.144.2, 사용자 결정): 화면 숫자는 마지막으로 센 때의 것이라 밖에서 디스크가 바뀌면
  (git reset 등) 화면 0·실제 N 일 수 있다. 토스트의 수는 `clear()` 가 돌려주는 `(기준점, 비운 수)` 의 수 — 새
  기준점으로 찍는 **같은 트리**로 `report(disk)` 가 센 것이라 셈과 기준 잡기 사이에 틈이 없다. 웹 응답 `cleared`.
- **꺼진 까닭**: runtime 은 실패한 저장소를 남기지 않는다(도구 광고·훅이 `get_snapshot` 을 본다). 대신
  `reset_snapshot(off=(cause, detail))` 로 까닭만 남기고 웹은 `snapshot_off()` 를 읽는다. cause 는 닫힌 어휘
  `OFF_CAUSES` — 발생 지점이 정한다: `disabled`(노브) · `git_missing`·`git_broken`(`_probe_git`) ·
  `init_failed`(`git init` 실패·init 중 예외, 까닭이 남지 않은 채 저장소가 없을 때도) · `unreadable`(저장소는 있으나
  기준점·목록을 못 셈 — 웹이 정한다). 화면 문장은 웹 서버의 표 `CHANGES_OFF_TEXT` 한 곳이고(키가 `OFF_CAUSES` 와
  같은지 테스트가 본다), 원문(`detail` — git stderr·예외)은 줄의 title 에만. payload `{enabled: false, cause,
  reason, detail}`.

웹(`/api/changes`·`/diff`·`/clear`·`/undo`)은 저장소 메서드를 실행기에서 부르는 얇은 층이고, 프런트 `js/changes.js`
는 페이지 로드·패널 열기·↻·런 끝(main 은 `worker_state` 바쁨→한가, 그 밖은 `scope_end`)에만 다시 센다.
테스트 `tests/test_snapshot_baseline.py`, `tests/test_web_server.py::TestChangesEndpoints`,
`tests/browser/test_changes_panel.py`.

## 8. 하위 git 저장소 안의 변경 — 웹 Changes 패널 (v10.139.0, 2026-11-15)

§3.3 대로 그림자 저장소는 하위 저장소를 gitlink 로만 담는다. 그래서 Changes 패널은 하위 저장소 안을 **그 저장소의 git 으로**
센다 — 하위 저장소 전체를 복사하지 않고, 기준점마다 *기록* 하나를 남긴다(`agent_cli/snapshot_nested.py`, 0층, `snapshot.py`
만 import 한다; 스냅샷 저장소 git 은 주입받는다). 모델이 받는 것은 바뀌지 않는다(`notice()`·`nested` 그대로).

- **기록**: 부모 없는 커밋 하나 — 트리 `{manifest.json, t/<i>}`. manifest 는 저장소마다 `RepoRecord`(경로·종류 `repo`/`plain`·
  HEAD·첫 커밋들 `rev-list --max-parents=0`·추적 파일 수·그때 dirty 였던 경로·담아 둔 트리·기준 시각·출처
  `baseline`/`new`/`reset`)와 안내 `Notice`(저장소·원인·시각·잃은 HEAD). dirty 파일 내용(수정·untracked)은
  `hash-object -w --no-filters` 로 우리 저장소에 담아 트리로 묶는다(`dirty` 에 있는데 트리에 없으면 "그때 없었음").
  트리가 `t/` 아래 매달려 ref 하나가 전부를 gc 에서 붙잡는다.
- **짝**: 세션 시작 기준 ↔ `refs/agent-cli/nested/start/<sid>`(그 세션 **첫** 시작에만 — 방금 찍은 시작 커밋이 가장 이른
  시작일 때; resume 은 덮어쓰지 않는다), Clear 기준 ↔ `refs/agent-cli/nested/baseline`(Undo 한 단계 `…/baseline-prev`).
  Clear·Undo 는 기준점 ref 와 함께 `update-ref --stdin` 한 트랜잭션. HEAD·실제 색인·`_last_checkpoint` 불변. 짝이 없는 기준
  (HEAD 기준·기록 이전의 옛 세션·옛 Clear)이면 지금 동작 — gitlink 한 줄.
- **셈**: 기록한 HEAD ↔ 작업 트리를 하위 git 으로(`diff --no-renames -z --raw/--numstat <head>` — 사이 커밋 + 커밋 안 한
  수정이 한 번에), 새 파일은 `ls-files --others --exclude-standard`(그 저장소의 `.gitignore`). 기록 때 dirty 였던 경로·
  untracked·추적에서 빠졌는데 남은 경로는 우리 저장소에서 "담아 둔 트리 ↔ 지금 내용 트리" 로 비교한다(같은 git 의
  numstat·unified diff). 경로는 워크스페이스 기준(`--src-prefix=a/P/`). 워크스페이스 쪽 diff 는 기록이 있는 경로를
  `:(exclude,literal)P` 로 뺀다. `EXCLUDE_DIRS`·`file_max_bytes` 는 같은 뜻(하위 pathspec·크기 거름). 하위 안의 또 다른
  저장소는 그 저장소 쪽에서 gitlink 한 줄, `.git` 이 파일이면(worktree·submodule) 하위 git 이 처리한다.
- **판정**(순수 함수 `judge` → `Verdict`, 처리는 `NestedRepos.reconcile` 의 작은 분기 — 판정이 일곱으로 닫혀 있어 상태
  클래스 계층은 두지 않는다): HEAD 있음 → 평소 / HEAD 없고(`cat-file -e`) 첫 커밋 겹침 → 기준 잃음(그 저장소만 지금으로
  다시 기록 + 안내) / 첫 커밋 다름 → 다시 clone(같은 처리, 원인만 다름) / 폴더 없음 → `D P/` 요약 한 줄 / 폴더 있는데
  저장소 아님 → 일반 폴더 기록(그 순간의 그 폴더 트리가 기준, 워크스페이스 그룹으로; 안내) / 기록에 없는 저장소(지금
  트리의 새 gitlink) → 그 순간으로 기록하고 `A P/` 요약 한 줄. 기록을 고치는 것은 **`GitSnapshot.sync_nested_records`
  하나뿐**이다 — 목록(`report`·`changes`)과 diff(`change_diff`)는 읽기만 한다(우리 저장소에 내용 객체를 쓰는 것은 트리를
  만드는 방법일 뿐, ref·색인·기록은 그대로). 웹은 `GET /api/changes`·Undo 에서 셈 앞에 그것을 부른다 — "발견한 순간" 이
  곧 패널을 센 순간이다. 겨룸은 잠금 대신 `update-ref <ref> <new> <old>`, 지면 다시 읽는다.
- **하위 저장소는 읽기만**: 하위 git 호출은 `GIT_OPTIONAL_LOCKS=0`·`core.fsmonitor=false`·`--no-ext-diff`·`--no-textconv`.
  색인을 읽는 호출(`diff`·`status`·`ls-files`)은 하위 색인의 우리 쪽 사본(`GIT_INDEX_FILE`, 스냅샷 저장소 안
  `index.sub-<key>`)을 읽는다 — porcelain `git diff <commit>` 은 `GIT_OPTIONAL_LOCKS=0` 이어도 stat 이 바뀐 파일을 보면
  색인을 고쳐 쓴다(실측 40회 중 8회). 사본은 셈 사이에 남아 stat 캐시가 이어지고, 원본의 크기·mtime·inode 가 바뀌면
  다시 복사한다(mtime 까지).
  우리 쪽 env 에도 `GIT_OPTIONAL_LOCKS=0` — gitlink 를 담는 `add` 가 하위 저장소에 `status` 를 띄우고 그 status 가 하위
  색인을 고쳐 쓰는 것을 실측으로 잡았다. 일반 폴더가 된 자리는 임시 색인에서만 남은 gitlink 를 지우고 담는다(`add -A` 는
  남은 gitlink 아래를 담지 않는다 — 체크포인트의 실제 색인은 그대로; 별도 과제).
- 웹: `/api/changes` 에 `files[].repo`·`summary`, `repos`(그룹 머리), `notices`(안내 줄), `POST /api/changes/notices/dismiss`.
  테스트 `tests/test_snapshot_nested.py`(하위 `.git` 지문 불변 검사 포함), `TestChangesEndpoints`, `tests/browser/test_changes_panel.py`.


## 9. 스냅샷 빈틈 — 저장소 경계와 조용한 실패 (v10.139.1, 2026-11-15)

하위 저장소 작업 중 찾은 기존 문제 넷. 넷 다 조용히 망가져 모델의 `snapshot` 복구와 Changes 패널이 함께 영향을 받았다.
모델이 받는 바이트는 바뀌지 않는다(시작 안내는 이미 커밋 없는 저장소도 나열한다) — 실패하던 체크포인트·restore 가 성공할 뿐이다.

- **커밋 없는 하위 저장소**: `add` 가 `'<p>/' does not have a commit checked out` 으로 **통째로** 실패했다 — 세션 시작 커밋부터
  없어 restore 는 "no snapshot contains …", 패널은 "Snapshots are off". 이제 `GitSnapshot._stage_workspace` 가 그 줄(`LC_ALL=C`
  로 띄워 언어 무관)을 읽어 그 경로를 **그 호출에서만** `:(exclude,literal)P` 로 빼고 다시 한다(git 은 한 번에 하나씩 거부한다 —
  상한 `ADD_RETRY_MAX`). 실패 메시지가 곧 목록이라 미리 걷지 않는다(평소 비용 0). 체크포인트·세션 시작·Changes 임시 색인이 같은
  함수를 쓴다. 판정 `Verdict.UNBORN` — 기록에 없거나 기록 때도 커밋이 없던 저장소가 지금도 커밋이 없으면; 첫 커밋 뒤 `NEW`
  ("first seen" 흐름). 패널 안내 줄 "P is a git repository with no commits yet — its files are not part of the list until its
  first commit." 은 셀 때마다(뺀 경로에서) 정하고 기록에 남기지 않는다(`dismissable: false`).
  - 이미 일반 파일로 담던 폴더에 `git init` 만 한 경우는 실측으로 `add` 가 실패하지 않는다 — 색인에 그 아래 항목이 있으면 git 은
    그 폴더를 저장소가 아닌 디렉터리로 내려간다. 그래서 넘기지 않고 계속 담는다(빼는 것이 없으니 얼어 붙는 파일도 없다).
    판정도 `plain` + 커밋 없음 → `PLAIN`.
- **`.git` 을 지운 폴더**: 스냅샷 색인에 gitlink 가 남아 `add -A` 가 그 아래를 담지 않았다(체크포인트에 안 파일이 영영 없음).
  이제 체크포인트·Changes 셈 직전 `GitSnapshot._align_index_with_repos` 가 그 gitlink 를 색인에서 뺀다 — 그러면 `add` 가 안 파일을
  일반 파일로 담는다. 그 자리가 지우고 다시 `git init` 만 한 저장소여도 뺀다(남은 gitlink 는 옛 커밋을 가리킨 채 그대로 담긴다 —
  실측); 그러면 위의 커밋 없는 저장소 길로 간다. 실제 색인은 체크포인트의 `_lock` 안에서만, HEAD·히스토리 불변.
  v10.139.0 의 Changes 쪽 임시 처리(`_disk_tree(drop)`·Clear 의 일반 폴더 기록)는 이 길로 합쳤다 — Clear 의 새 기준점 트리에 그
  폴더가 일반 파일로 담기므로 일반 폴더 기록이 따로 필요 없다.
- **저장소가 된 폴더**: 일반 파일로 담던 폴더에 커밋 있는 저장소가 생기면(`git init` + 첫 커밋, 또는 그 자리에 clone) 같은
  `_align_index_with_repos` 가 그 아래 항목을 스냅샷 색인에서만 뺀다(디스크·그 저장소 불변) → `add` 가 gitlink 하나로 담는다.
  발견은 색인에 있는 디렉터리마다 `<dir>/.git` 확인(바깥부터, 찾은 저장소 안은 묻지 않는다), 커밋 유무는 찾은 것만 하위 git 에
  묻는다. 무엇을 뺄지는 `snapshot_nested.index_paths_to_untrack`(읽는 함수는 주입). 예전 체크포인트에는 넘기기 전 판이 남으므로
  모델의 `restore` 는 그 판으로 계속 된다.
  Changes 연속성: `sync_nested_records` 가 지금 트리의 새 gitlink 를 발견했을 때 기준점 트리의 그 자리가 일반 폴더였으면
  `NestedRepos.record_adopted` 로 기록한다 — 기준 HEAD = 지금 HEAD, dirty 덮어쓰기 = 기준점 폴더 트리의 blob 중 HEAD 와 다른 것
  (이미 우리 저장소에 있는 blob 을 가리킨다 — 복사 없음)과 HEAD 에만 있는 경로("그때 없었음"). 그래서 목록은 "기준점 이후 실제로
  바뀐 파일" 로 이어진다(요약 한 줄이 아님, 출처 `adopted`, 그룹 머리는 평소). 기준점에 그 폴더가 없었으면 기존 "first seen".
  일반 폴더 기록(§8 탭 6 뒤)이 다시 커밋 있는 저장소가 되면 그 기록의 트리로 같은 방법을 쓰고, 낡은 ".git was removed" 안내는 거둔다.
- **조용한 실패**: `checkpoint()` 가 "변경 없음" 과 "실패" 를 둘 다 None 으로 돌려 구별이 없었다. 이제 실패(`add`·`commit`)면
  `stats.failing_since`(첫 실패 시각)·`failure`(짧은 이유, `git add: <첫 오류 줄>`)를 남기고, 성공(변경 없음 포함)하면 지운다.
  `report()` 는 그 동안 빨간 안내 줄 "Snapshots have been failing since HH:MM (<이유>). Files changed after that may be missing from
  this list and cannot be restored."(`cause` `snapshot_failing`, `repo` null, `dismissable: false`)을 맨 앞에 싣는다. 이번 셈의
  `add` 도 실패하면(그래서 예전엔 목록이 None → "Snapshots are off") 마지막 스냅샷(HEAD)까지의 목록 + 그 안내. 모델에게 알릴지는
  별개 결정이라 이번엔 안 한다.
- 테스트 `tests/test_snapshot_gaps.py`(모든 호출 앞뒤 하위 `.git` 지문 불변), `TestChangesEndpoints`, 브라우저
  `test_unborn_repo_and_failing_snapshots_notices`.
