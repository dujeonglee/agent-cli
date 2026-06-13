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
git status            # 작업 트리 클린 (untracked scratch 제외)
```

README.md / docs/ARCHITECTURE.md가 최신인지 확인.

### 2. 버전 bump

`agent_cli/__init__.py`의 `__version__`을 `X.Y.Z`로 변경. 확인:

```bash
agent-cli --version   # → agent-cli X.Y.Z
```

### 3. CHANGELOG 갱신

`CHANGELOG.md`의 `[Unreleased]` 아래에 `## [X.Y.Z] - YYYY-MM-DD` 섹션을 만들고
Added / Changed / Fixed / Removed로 정리. 하단 compare 링크도 갱신.

### 4. 릴리스 커밋 (브랜치 → 머지 → 푸쉬)

```bash
git checkout -b release/vX.Y.Z
git add agent_cli/__init__.py CHANGELOG.md   # + 동반 문서
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
  --notes-file <(awk '/^## \[X.Y.Z\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md) \
  dist/agent_cli-X.Y.Z*
```

## 설치 (사용자)

```bash
# 태그된 릴리스에서 직접
pip install "git+ssh://git@github.com/dujeonglee/agent-cli.git@vX.Y.Z"

# 또는 GitHub Release에 첨부된 wheel
pip install agent_cli-X.Y.Z-py3-none-any.whl

# 웹 UI 포함
pip install "agent-cli[web] @ git+ssh://git@github.com/dujeonglee/agent-cli.git@vX.Y.Z"
```
