# 모델을 언제 고르는가 — 해석과 검증

> 상태: **구현 완료** (v9.5.0) · 2026-09-19 사용자와 공동 결정
> 검토 시안: https://claude.ai/code/artifact/95259759-8b7f-4a41-b38e-b6b22ac99412

## 1. 무엇이 고장났었나

사용자 제보. 상주 에이전트에게 "3+3은 뭐야"를 물었는데 대화 한복판에 빨간
거부 카드가 떴고, 로그에는 이게 있었다:

```
404: Model 'Qwen-3.8-Flash-Next-Uncensored-MLX-MXFP4' not found.
Available models: Qwen3.6-35B-A3B-8bit, Qwen3.8-Flash-Next-oQ4e-mtp
```

서버가 모델을 교체했는데 `~/.agent-cli/config.json` 의 `default_model` 이
옛 이름에 남아 있었다. **그 이름을 아무도 확인하지 않아서 첫 LLM 호출까지
살아남았다.**

## 2. 두 층의 문제

```
--model  >  config.json: default_model  >  패키지 default_models.json
                                           openai → "gpt-4o"
                                           anthropic → "claude-sonnet-4-20250514"
```

**패키지 추측은 거짓말이었다.** 사용자는 `127.0.0.1:8000` 의 로컬 MLX 서버를
쓴다. 거기 `gpt-4o` 가 있을 리 없다. 이 폴백은 **절대 맞을 수 없으면서**
"모델을 안 골랐다"를 "없는 모델 404"로 바꿔 원인을 가렸다.

**설정값은 조용히 썩었다.** 한 번 적히면 다시 확인되지 않는다.

## 3. 검토한 갈래

사용자 제안은 "`default_model` 개념을 없애고 매번 명시하게 하자"였다.
여섯 장면으로 비교했고(위 시안), 결론은 **1안 — 기본값은 남기고 검증한다**.

판단의 축은 하나였다. **명시 강제는 staleness 를 고치지 못한다.**
`--model` 을 매번 쳐도 오타 나거나 없어진 모델이면 똑같이 404다. 고장의
원인은 "기본값이 있다"가 아니라 **"그 이름을 아무도 확인하지 않는다"** 였다.

| | 1안 (채택) | 2안 (기각) |
|---|---|---|
| 오늘의 404 | 고쳐짐 | 고쳐짐 |
| 오타·죽은 모델 | 부팅 차단 | 부팅 차단 |
| **평소 실행** | 타이핑 0 | 매번 모델명 |
| **목록 못 읽는 환경** | 저장값으로 진행 | 맹목 타이핑 |
| agent-board | 무변경 | 스키마·폼 변경 |

2안이 사는 것은 "지금 무슨 모델로 도는지 항상 의식한다" 하나인데, 그건
부팅 첫 줄에 모델을 찍으면 검증이 대신 준다.

## 4. 설계 — 삼치(三値)가 핵심

조회 결과를 **둘이 아니라 셋**으로 본다. 이게 이 설계의 전부다.

```
목록 받음 + 이름 있음   →  진행
목록 받음 + 이름 없음   →  실패          (고칠 방법을 알려줄 수 있다)
목록을 못 받음          →  경고 후 진행   ★
```

★ 가 없으면 오프라인·`/models` 미지원 서버에서 부팅이 막힌다 — **원래
고장보다 나쁜 회귀**다. 구 `setup._list_models` 는 실패를 전부 `[]` 로
뭉갰기 때문에 그 모양을 그대로 쓸 수 없었고, 그래서 `model_check` 가
따로 생겼다.

**확인할 수 없는 것을 틀렸다고 단정하지 않는다.**

## 5. 구현

`agent_cli/model_check.py` (신규)

- `ModelListing(models, available, reason)` — `available=False` 면 `models`
  는 무의미하다("0개"가 아니라 "묻지 못했다")
- `list_models()` — `(base_url, provider)` 키로 프로세스 캐시. 부팅 1회 +
  spawn 마다 같은 목록을 다시 받지 않는다. api_key 는 캐시 키에서 뺀다.
- `verify_model()` — 위 삼치를 예외/반환으로 표현
- `suggest()` — `difflib` 근접 이름 (오타 구제)
- `is_interactive()` — 보드가 띄운 인스턴스는 stdin 이 파이프라 물으면
  **멈춘다** (v9.1.0 에서 `agent-cli mcp` 가 겪은 함정)

**검증 지점 둘.**

1. `main._setup_provider` — 모든 진입점(run/web)이 지나는 단일 지점.
   `_resolve_provider` 직후, provider 생성 **전**.
2. `AgentRegistry.spawn` — 역할 md 의 `model:` 이 상속값을 덮었을 때만.
   상속분은 부팅 때 이미 검증됐다. 안 보면 그 에이전트의 **첫 턴에 가서야**
   404 가 드러난다 — 제보된 증상이 정확히 이 경로였다.

**제거.** `_PROVIDER_FALLBACKS` 의 모델 이름과 `default_models.json` 의
`provider_defaults.*.default_model`. 주소는 남긴다 — 프로바이더의 공개
엔드포인트는 실제로 그 주소가 맞다. 모델 이름만이 추측이었다.

## 6. 실패 화면

```
✗ 설정된 모델을 서버에서 찾을 수 없습니다.
  모델  Qwen-3.8-Flash-Next-Uncensored-MLX-MXFP4
  비슷한 이름  Qwen3.8-Flash-Next-oQ4e-mtp
  서버  http://127.0.0.1:8000/v1
  출처  /Users/…/.agent-cli/config.json

  사용 가능:
    1) Qwen3.6-35B-A3B-8bit
    2) Qwen3.8-Flash-Next-oQ4e-mtp

  고르기 [1-2]  ← TTY 일 때만. 고르면 config.json 에 저장(다음부터 안 묻는다)
```

TTY 가 아니면 목록까지만 보이고 `exit 1`. **출처**를 찍는 이유는 고칠 파일을
지목하기 위해서다 — `--model`, config 경로, 또는 역할 md 이름.

## 7. 남은 것

- `setup._list_models` 와 `model_check.list_models` 가 같은 엔드포인트를
  각자 친다. 위저드는 실패를 수동 입력으로 흡수하면 그만이라 구분이
  필요 없어 지금은 합치지 않았다. 합칠 거면 위저드를 `ModelListing` 쪽으로
  옮기는 방향(반대가 아니라).
- 경고 문구의 `reason` 은 분류만 준다(`연결할 수 없음` 등). 원문은 urllib3
  내부 사정이라 화면 절반을 먹었다(실장에서 확인).
