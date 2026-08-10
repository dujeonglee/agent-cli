# First-use study 샘플 리포 (진행자 전용 — 참가자에게 보여주지 말 것)

> `22-study-run-kit.md` §5 의 과제 카드가 가리키는 연습용 리포. **참가자에게는
> `repo-template/` 의 사본만 준다** — 이 README 는 심어 둔 결함의 정답지다.

## 팀별 사본 만들기

과제쌍마다(=조건마다) 새 사본. 한 팀 기준:

```bash
for pair in pair1 pair2 pair3; do
  cp -r bench/study/repo-template study-data/team<N>/repo-$pair
done
# 서버는 해당 사본 디렉토리에서 기동한다 (킷 §2)
```

동작 확인(사본에서): `python cli.py add "hi" --priority 2 && python cli.py list`,
테스트는 `python -m pytest test_validate.py -q` (5개 통과가 정상).

## 리포 내용물 (약 200 LOC)

TaskBook — 작업 목록 CLI. `cli.py`(argparse 진입점) · `validate.py`(입력 검증)
· `storage.py`(JSON 저장) · `config.py`(설정 파서) · `taskbook.conf` ·
`test_validate.py` · `README.md`(의도적으로 낡음).

## 정답지 — 심어 둔 결함과 카드 매핑

| 카드 | 대상 파일 | 심어 둔 상태 (검증 완료) |
|---|---|---|
| 1-P1 경계 버그 | `validate.py` (+`test_validate.py`) | 스펙은 docstring: 제목 1~50자, 우선순위 1~5. 실제로는 ① 공백뿐인 제목이 `''` 로 통과 ② `> 51` 오프바이원으로 51자 통과 ③ `> 6` 으로 우선순위 6 통과. 기존 테스트 5개는 경계를 안 봄 |
| 1-P2 README 갱신 | `README.md` | 진입점이 `taskbook.py new/show`(실제는 `cli.py add/list/done`), `-p`(실제 `--priority`), 존재하지 않는 `requirements.txt`, `done` 명령 누락 |
| 2-P1 `--json` 옵션 | `cli.py` | `list` 가 텍스트 출력만. `--json` 추가 여지가 cmd_list 에 있음 |
| 2-P2 구조화 로그 | `storage.py` | `print("DEBUG: ...")` 3곳 + 오류 print 1곳 — 레벨·타임스탬프 없는 원시 출력 |
| 3-P1 설정 기본값 | `config.py` | docstring 에 기본값 3종(db_file/date_format/max_open) 문서화, 구현은 `settings[key]` 라 누락 키에서 raw KeyError. `taskbook.conf` 는 일부 키 고의 누락 |
| 3-P2 오류 메시지 정비 | `cli.py`·`validate.py`·`storage.py` | 형식이 제각각: `"ERROR!! …"`, `"[TaskBook] …"`, `"Error: no such task!!!"`, `"bad priority (…)"`, `"error: cannot read …"` |

**쌍 내 파일 겹침 없음**(realistic 원칙): 1(P1 validate/test ↔ P2 README),
2(P1 cli ↔ P2 storage), 3(P1 config ↔ P2 cli+validate+storage). 카드 3-P2 는
`config.py` 를 대상에서 명시적으로 제외한다(3-P1 과의 겹침 방지).

## 인터뷰 채점에 쓰는 법

- M1 정지 탐침 채점: 상대 턴의 실제 작업은 세션 `history.jsonl` 의
  `reply_to`×`files` 로 확인 (킷 §6-1).
- 모듈 C(스코핑 off) 오염 판정 기준도 동일 — 위 표의 "대상 파일" 이 곧
  각 참가자의 영역 경계다.
