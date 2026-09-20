# 릴리스 가이드

agent-cli의 버전 정책과 릴리스 절차. on-premise 배포 대상이며 **PyPI에 publish하지
않습니다** — 릴리스는 git 태그 + GitHub Release(+ 빌드 산출물 첨부)로 구성됩니다.

## 버전 정책 (SemVer)

`MAJOR.MINOR.PATCH` — [Semantic Versioning](https://semver.org/).

| 증가 | 기준 |
|------|------|
| **MAJOR** | CLI 플래그/설정(`config.json`) 스키마 호환 깨짐, **기본 wire format 전환**, 도구 입력 스키마 파괴적 변경 |
| **MINOR** | 하위호환 기능 추가 — 새 도구, 새 CLI 옵션, 새 wire format, 새 스킬/에이전트 |
| **PATCH** | 버그 픽스, 문서, 내부 리팩터(동작 불변) |

버전의 **단일 소스는 `agent_cli/__init__.py`의 `__version__`** 입니다.
`pyproject.toml`은 `dynamic = ["version"]`으로 이 값을 읽습니다 — 한 곳만 고치면 됩니다.

## 릴리스 절차

`X.Y.Z`를 올릴 버전으로 치환.

### 1. 사전 점검 (그린이어야 함)

```bash
pytest tests/ -m "not ollama_integration"
ruff check agent_cli/ tests/
ruff format --check agent_cli/ tests/
node --check agent_cli/web/static/app.js      # 프런트 수정 시
git status            # 작업 트리 클린 (untracked scratch 제외)
```

**프런트(`web/static/`)를 건드렸다면 추가로**:

```bash
AGENT_CLI_BROWSER_TESTS=1 pytest tests/browser/          # 실브라우저 e2e + 성능 회귀 가드
# 렌더 경로를 건드렸다면 KPI 도 측정해 docs/PERF-KPI.md 표 갱신
AGENT_CLI_BROWSER_TESTS=1 AGENT_CLI_PERF_KPI=1 pytest tests/browser/test_render_perf.py -s
```

성능 KPI 의 정의·베이스라인·회귀 판정 기준은 [docs/PERF-KPI.md](docs/PERF-KPI.md).

README.md / docs/ARCHITECTURE.md가 최신인지 확인.

### 1-1. CI 가 그린인지 확인 — **로컬 통과로는 부족하다**

```bash
gh run list --limit 3          # 직전 push 들이 success 인가
gh run view <id> --log-failed  # 아니면 원인부터
```

로컬은 macOS 한 대지만 CI 는 **Linux × Python 3.11/3.12** 다. 플랫폼 가정이
섞인 테스트는 로컬에서 영영 안 보인다 — v9.10.0~v9.11.1 세 릴리스가
`/private/etc`(macOS 전용)를 전제한 TC 하나 때문에 **전부 빨간 상태로
나갔다.** 태그를 밀기 전에 직전 커밋의 CI 를 보는 것이 그 부류를 잡는
유일한 지점이다.

플랫폼 의존 테스트에는 반드시 `@pytest.mark.skipif(sys.platform != ...)` 를
붙인다.

### 2. 버전 bump

`agent_cli/__init__.py`의 `__version__`을 `X.Y.Z`로 변경. 확인:

```bash
agent-cli --version   # → agent-cli X.Y.Z
```

### 3. 릴리스 노트 작성

**`CHANGELOG.md` 는 v9.0.0 이후 쓰지 않습니다** — 릴리스 노트는 GitHub Release
본문이 단일 소스이고, 재료는 이번 태그 이후의 커밋 메시지입니다
(`git log --oneline vX.Y.(Z-1)..HEAD`). 6단계에서 `--notes-file` 로 붙입니다.

### 4. 릴리스 커밋 (브랜치 → 머지 → 푸쉬)

```bash
git checkout -b release/vX.Y.Z
git add agent_cli/__init__.py   # + 동반 문서(README·ARCHITECTURE·PERF-KPI)
git commit -m "chore(release): vX.Y.Z"
git checkout main && git merge --ff-only release/vX.Y.Z
git push origin main
git branch -d release/vX.Y.Z
```

### 5. 태그

```bash
git tag -a vX.Y.Z -m "agent-cli X.Y.Z"
git push origin vX.Y.Z
```

### 6. 빌드 + GitHub Release

```bash
python -m build                       # dist/agent_cli-X.Y.Z-py3-none-any.whl + .tar.gz
gh release create vX.Y.Z \
  --title "vX.Y.Z" \
  --notes-file RELEASE_NOTES.md \
  dist/agent_cli-X.Y.Z*
```

## 설치 (사용자)

설치 후 업데이트는 `agent-cli update`(gh 로 최신 릴리스 wheel 받아 pip 업그레이드, `--check` 로 확인만).

```bash
# 태그된 릴리스에서 직접
pip install "git+ssh://git@github.com/dujeonglee/agent-cli.git@vX.Y.Z"

# 또는 GitHub Release에 첨부된 wheel
pip install agent_cli-X.Y.Z-py3-none-any.whl

# 웹 UI 포함
pip install "agent-cli[web] @ git+ssh://git@github.com/dujeonglee/agent-cli.git@vX.Y.Z"
```
