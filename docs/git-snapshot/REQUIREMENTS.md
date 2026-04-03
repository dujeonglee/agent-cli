# Git 상태 스냅샷 — 요구사항 문서

> 작성일: 2026-04-04
> 상태: 리뷰 대기

---

## 1. 배경

현재 시스템 프롬프트의 Environment 섹션에는 작업 디렉토리, 날짜, 플랫폼 정보만 포함된다.
에이전트가 Git 저장소 내에서 작업할 때:
- 현재 브랜치, 스테이지된 변경, unstaged 변경 등의 Git 상태를 파악하려면 매번 `shell` 도구로 `git status`/`git diff`를 실행해야 함
- 첫 턴에 Git 상태 파악을 위한 불필요한 이터레이션 소모
- 사용자가 "현재 변경사항을 리뷰해줘" 같은 요청을 할 때 맥락 부족

## 2. 목표

세션 시작 시 Git 상태 정보를 시스템 프롬프트에 자동 주입하여:
1. 에이전트가 첫 턴부터 현재 Git 상태를 인지
2. 불필요한 도구 호출(git status, git diff) 감소
3. 컨텍스트 인지 응답 품질 향상

## 3. 기능 요구사항

### 3.1 Git Context 섹션 추가

- `build_system_prompt()`에서 Environment 섹션 뒤, Directives 섹션 앞에 `## Git Context` 섹션 추가
- Git 저장소가 아닌 디렉토리에서는 섹션 전체 생략

### 3.2 포함 정보

| 항목 | 명령어 | 설명 |
|------|--------|------|
| 브랜치 + 트래킹 상태 | `git status --short --branch` | 현재 브랜치명, ahead/behind 등 |
| Diff (staged + unstaged) | `git diff HEAD` | 작업 중인 모든 변경 내용 |

- `git status --short --branch` 결과는 전체 포함 (파일 목록은 보통 짧음)
- `git diff HEAD` 결과는 예산 제한 적용 (3.3 참조)

### 3.3 Diff 예산 제한

- 최대 diff 문자 수: **4000자** (상수로 정의, `MAX_GIT_DIFF_CHARS`)
- 초과 시 diff를 4000자까지 잘라내고 `[diff truncated — {total}chars total]` 메시지 추가
- diff가 빈 경우 (변경 없음) diff 부분 생략, status만 표시

### 3.4 실행 조건

- **1회 빌드**: `build_system_prompt()` 호출 시 1회 실행. 매 턴 갱신 아님 (시스템 프롬프트는 세션 시작 시 한 번 빌드됨)
- **Git 미설치**: `git` 바이너리가 PATH에 없으면 섹션 생략
- **Git 저장소 아님**: `git status` 실행 실패 시 (exit code != 0) 섹션 생략
- **타임아웃**: Git 명령이 3초 이내에 완료되지 않으면 섹션 생략 (대형 저장소 방어)

### 3.5 출력 포맷

```
## Git Context
$ git status --short --branch
## main...origin/main
 M src/auth.py
?? new_file.py

$ git diff HEAD
diff --git a/src/auth.py b/src/auth.py
index abc1234..def5678 100644
--- a/src/auth.py
+++ b/src/auth.py
@@ -10,3 +10,5 @@ def login():
     ...
[diff truncated — 12345chars total]
```

## 4. 비기능 요구사항

### 4.1 성능

- Git 명령 실행은 `subprocess.run`으로 동기 실행, 3초 타임아웃
- 두 명령(`git status`, `git diff`)은 순차 실행 (병렬화 불필요 — 총 실행시간 << 100ms 예상)

### 4.2 보안

- `subprocess.run`에 `shell=False` 사용 (command injection 방지)
- Git 명령의 stdout만 사용, stderr는 무시

### 4.3 호환성

- Git이 설치되지 않은 환경에서 에러 없이 정상 동작 (섹션 생략)
- Windows/macOS/Linux 모두 지원 (`shutil.which("git")` 사용)

## 5. 범위 외

- 매 턴 Git 상태 갱신 (성능 이유로 제외)
- `git log` 출력 (히스토리는 범위 과다)
- `.gitignore` 파일 내용 주입
- Git 서브모듈 정보
- 사용자 설정으로 기능 on/off 토글 (향후 확장)
