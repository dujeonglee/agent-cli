# 이미지 입력 (vision) — 설계

> 상태: **P1 출시 (v10.59.0, 2026-10-21)** — 두 공급자·`read_file`·CLI `--image`·능력 프로브·토큰·살아 있는 수 상한·재개. P2(웹)·P3(screenshot·MCP·축소 실측)는 §8. §9 의 결정은 2026-10-21 에 사용자가 내렸다 — §11.

## 1. 왜

모델이 볼 수 없는 것이 셋 있다 — 사용자가 보여 주는 화면(버그 캡처, 도면, 표 사진),
fetch 가 `.agent-cli/tmp/downloads/` 에 받아 둔 이미지, 브라우저가 그린 화면.
지금은 `read_file` 로 PNG 를 열면 `UnicodeDecodeError` 로 실패하고, 웹·CLI 어디에도
이미지를 건넬 길이 없다. 코드 전체에 `image_url` 이 한 번도 나오지 않는다.

로컬 모델(Qwen3.6-35B-A3B, `vision_config` 보유)과 omlx 는 이미지를 받는다 — 0층 실측:

| 실측 (scratchpad `vision/probe.py`) | 결과 |
|---|---|
| user 메시지 `image_url`(data URL) 480×240 도형·글자 | 정확 |
| **tool 역할 메시지 안의 이미지** | **조용히 버려짐** — 토큰 안 늘고 파일명으로 추측 |
| tool 결과는 글, 이미지는 **다음 user 메시지** | 정확 |
| 한 메시지에 두 장 | 각각 구별 |
| 토큰 480×240 / 1280×720 / 1920×1080 | ≈130 / ≈890 / ≈2,050 (픽셀 ∝, ≈1K px/token) |
| 60px 글자 판독 | 1280×720 두 번 오독("KW400"), 1920×1080 정확 — 축소 정책은 실측 뒤 |

## 2. 원칙

1. **`content` 는 끝까지 문자열.** 레코드·`messages`·꼬리·압축·재생·표시가 전부 `content: str`
   을 가정한다(`manager.py:614` `last["content"] + tail`, `dispatch.py:657` 결합,
   `render.py:64` `_message_texts` 는 list 를 0 토큰으로 센다, `records.py:76`,
   `session.py:216`, `web.py:1145`). 이 가정을 바꾸지 않는다 — 이미지는 **별도 필드**로
   레코드에 실리고, **공급자 경계에서만** 조각(parts)으로 펼친다.
2. **파일이 진실, 기록은 참조.** history.jsonl 에는 base64 가 아니라 경로·크기·해시만.
   보낼 때 파일에서 읽고, 파일이 없으면 글 자리표시로 대신한다.
3. **공급자마다 자기 모양.** OpenAI 는 `image_url` data URL, Anthropic 은
   `{"type":"image","source":{"type":"base64",…}}`. 변환은 provider 안 한 함수.
   방언·컨텍스트·도구는 공급자 모양을 모른다.
4. **tool 메시지엔 이미지를 넣지 않는다.** omlx 가 버리고(실측) OpenAI 규격도 tool 은
   글만이다. 관찰이 `role:"tool"` 이면(native_fc) 이미지는 **바로 뒤 user 메시지**에 —
   꼬리가 이미 그렇게 간다(`manager.py:607-611`, "OpenAI 규격상 허용").
5. **없는 능력을 약속하지 않는다.** `supports_vision` 이 거짓인 모델엔 이미지를 보내지
   않고, 그 사실을 사용자와 모델 양쪽에 말한다(snapshot 도구의 광고 원칙과 같다).

## 3. 데이터

### 3.1 `ImageRef` — 레코드의 이미지 참조

```json
{"path": ".agent-cli/tmp/uploads/1792502779-shot.png",
 "mime": "image/png", "width": 1280, "height": 720, "bytes": 184302, "sha256": "…"}
```

- `path` 는 워크스페이스 기준 상대 경로(`display_path` 규칙). 워크스페이스 밖 파일은
  입력 단계에서 `.agent-cli/tmp/uploads/` 로 복사한다(세션과 함께 정리, 재개 때도 남아 있다).
- 크기는 헤더만 읽어 구한다(PNG·JPEG·GIF·WebP, stdlib) — Pillow 없이 동작.
- `sha256` 은 재개 때 "같은 파일인가" 확인용. 다르면 자리표시로 대체하고 알린다.

### 3.2 레코드 — `images` 필드

history.jsonl 의 user 레코드(질의·관찰)에 `images: [ImageRef]` 를 더한다. `content` 는
그대로 글이다. 글 쪽에도 자리표시 한 줄을 넣는다 — 이미지가 못 가는 경로(요약기·
텍스트 전용 모델·파일 소실·인스펙터 글 보기)에서 같은 레코드가 그대로 읽히도록:

```
[image: .agent-cli/tmp/uploads/1792502779-shot.png (1280×720)]
```

(번호는 뺐다 — 경로가 이미 유일하고, 여러 op 의 결과를 합칠 때 다시 매길 일이 없다.)

| 레코드 | 누가 넣나 | 예 |
|---|---|---|
| 질의(`kind: query`) | `_add_user_message(text, images=…)` — content 끝에 자리표시 줄 | 사용자가 붙인 캡처 |
| 관찰(`kind: observation`) | `_append_observation(…, images=result.images)` | `read_file` 이 연 PNG, browser 스크린샷 |
| 요약(압축 산물) | 없음 — 글로만 | `_to_summary_text` 가 자리표시 줄을 그대로 쓴다 |

### 3.3 `ToolResult.images`

`ToolResult(success, output, error, artifact)` 에 `images: list[dict] = []` (`ImageRef.to_dict()` 모양 — 레코드에 그대로 적힌다).
`output` 은 여전히 글(크기·형식·자리표시). 여러 op 를 합치는 `_flush_op_results` 는
`images` 를 이어 붙인다(글은 지금처럼 결합).

## 4. 요청 조립 — 공급자 경계

### 4.1 컨텍스트 → `messages`

`get_messages()` 는 지금처럼 `content: str` 메시지 목록을 만들되, 레코드에 `images` 가
있으면 **어느 메시지에 붙일지**만 정한다(`render_history_message` 의 `_with_images` /
`_with_observation_images`):

| 렌더된 마지막 메시지 | 붙이는 곳 |
|---|---|
| `role: user` (질의, 텍스트 방언의 관찰) | 그 메시지의 `images` |
| `role: tool` (native_fc 관찰) | 뒤에 `{"role":"user","content":"<자리표시 줄들>","images":[…]}` 추가 |

여기까지 이미지는 참조뿐이고 파일은 안 읽는다. `pair_call_results`(native_fc 가 빠진
tool 답을 채우는 자리)는 이 user 메시지를 "답 없는 호출"로 오인하면 안 된다 — 꼬리
메시지와 같은 취급.

**살아 있는 이미지 수 상한(`AGENT_CLI_IMAGE_KEEP`, 기본 6 — 결정, 0=무제한; `images.keep_recent`).** 오래된 레코드의
이미지는 `get_messages` 에서 참조를 떼고 자리표시 글만 남긴다 — 기록은 그대로.
한 장 ≈ 0.3~0.8% 창(FHD 2K 토큰/262K)이라 몇 장은 괜찮지만 영상 프레임 수십 장이면
창이 찬다. 압축이 레코드를 내보내면 자연히 사라진다.

### 4.2 provider — 조각으로 펼치기

`provider.call(messages, …)` 안, 본문을 만들기 직전에 공용 `images.expand(messages, enabled, part)`
(조각 생성자 `openai_part`/`anthropic_part` 만 공급자 것):

```python
# openai.py
{"role": "user", "content": [
    {"type": "text", "text": msg["content"]},
    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}, …]}
# anthropic.py
{"role": "user", "content": [
    {"type": "text", "text": msg["content"]},
    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}, …]}
```

- 파일은 여기서 읽는다(세션당 `(path, size, mtime) → (sha, mime, b64)` 캐시, 같은 이미지가
  턴마다 다시 인코딩되지 않게). `images` 키는 **항상** 떼어 낸다 — 서버에 모르는 키를 보내지 않는다.
- 파일 없음·해시 불일치·인코딩 실패 → 조각을 빼고 `content` 끝에
  `(image path not shown: <사유>)` 를 덧붙인다(레코드는 안 건드린다).
- 못 보는 모델(`vision_support` 거짓)이면 펼치지 않고 `content` 끝에 "이 모델은 이미지를
  못 본다 — 다른 길로(스크립트로 읽기)" 한 줄 — 모델이 봤다고 착각하지 않게.
- Anthropic 은 user 메시지 여러 개가 이어져도 받으므로(native_fc 가 Anthropic 에선
  부팅 거부라 실제론 텍스트 방언만) 4.1 의 "뒤에 user 추가" 경로는 OpenAI 전용이다.
- 요약기 호출(`llm.py:577`)과 directives(`web/directives.py`)는 `images` 를 안 싣는다.

### 4.3 토큰

- `_estimate_message_tokens` 가 `images` 를 센다: `pixels / IMAGE_PX_PER_TOKEN`
  (1,000 하나 — Qwen 실측 ≈1K; Anthropic 공식 ≈750 과의 차이는 서버 실측 재조정이
  흡수하므로 공급자별 상수는 두지 않았다).
- `request_bytes` 는 이미지 **글**만 센다(base64 를 넣으면 글 토큰/바이트 비율이 깨진다).
  `reconcile_actual_tokens` 는 서버 실측에서 이미지 추정 토큰을 뺀 값으로 비율을 맞춘다.
- 과대 출력 정책(`ToolBridge.observe`): 관찰의 이미지 토큰은 op 캡·턴 예산에 **들어간다**.
  이미지만으로 캡을 넘으면 이미지를 빼고 본문에 "축소해서 다시" 를 적는다(글은 작으니
  그대로). 글이 과대 경로를 타면 이미지도 싣지 않는다 — 자리표시 줄이 발췌에 남는다.

## 5. 들어오는 길

| 길 | 어디서 | 레코드 | 단계 |
|---|---|---|---|
| **사용자 — CLI** | `agent-cli run "…" --image a.png --image b.png` (`_stage_query_images` → 큐 아이템 `images` → `RunRequest.images` → `run_loop(query_images=)`) | 질의 | P1 ✅ |
| **사용자 — 웹** | 입력창에 붙여넣기(`paste` 의 `clipboardData.files`) · 끌어다 놓기 · 📎 버튼 → `POST /api/workspace/upload?path=.agent-cli/tmp/uploads/<ts>-<name>` → `POST /api/input {"kind":"chat","content":…,"images":[rel]}` | 질의 | P2 |
| **사용자 — 웹, 에이전트 채널** | `POST /api/agent/{key}/input` 같은 모양 | 질의 | P2 |
| **모델 — `read_file`** | 경로의 매직 바이트가 이미지면 글 대신 `images=[ref]`, `output` 첫 줄은 자리표시 `[image: a.png (1280×720)]`, 둘째 줄 `image/png, 184,302 bytes — attached` | 관찰 | P1 ✅ |
| **모델 — `browser`** | `mode: "screenshot"` → `.agent-cli/tmp/screenshots/<n>.png` 저장 + `images` | 관찰 | P3 |
| **모델 — fetch** | 이미 `downloads/` 에 저장 — 그다음 `read_file` 로 본다. 변경 없음 | — | — |
| **MCP 도구** | `adapter.py:116` 이 지금 base64 를 **글로** 넣는다(사실상 결함) → 파일로 쓰고 `images` | 관찰 | P3 |

- `read_file` 을 쓰는 이유: 모델이 지금도 PNG 에 `read_file` 을 부른다(실패 관찰이
  남아 있다). 새 도구 이름을 가르치는 것보다 있던 길을 넓히는 게 싸다. 이미지엔
  줄 범위·해시라인·`search` 가 무의미하므로 그 인자는 무시하고 결과에 적는다. 대안
  `view_image` 는 §9.
- 웹 서버 검증: `images` 의 경로는 워크스페이스 안이어야 하고(`_confine.resolve_within`),
  존재해야 하며, 매직 바이트가 이미지여야 한다. 아니면 400.
- 텍스트 전용 모델: 웹은 📎 를 숨기고 붙여넣기에 "이 모델은 이미지를 못 본다" 토스트,
  CLI `--image` 는 즉시 오류. `read_file` 은 성공 결과에
  `this model cannot view images (supports_vision=false); describe it another way`.

## 6. 크기와 축소

- 보내기 전 상한: 긴 변 `AGENT_CLI_IMAGE_MAX_EDGE`(기본 후보 2048 — Anthropic 권장
  ≤1568, OpenAI 고해상도 2048) · 바이트 상한(기본 후보 5MB, Anthropic 한도).
- 축소는 Pillow — **필수 의존성**(결정 2026-10-21): 설치에 따라 동작이 갈리는 경로를 두지
  않는다(mcp 를 필수로 바꾼 v8.62.0 과 같은 이유). 휠이 세 OS 에 다 있어 부담이 작다.
  크기 읽기도 Pillow(헤더만 지연 디코딩). 바이트 상한(5MB)을 넘으면 JPEG 로 재인코딩.
- 기본값은 §1 실측(1280×720 에서 60px 오독, FHD 정확)이 축소에 불리한 신호라
  **실측 뒤** 정한다: 같은 화면을 원본·2048·1568·1024 로 보내 글자 판독률을 잰다.

## 7. 표면

| 표면 | 무엇 |
|---|---|
| **웹 사용자 카드** | 썸네일(최대 높이 160px) + 파일명·크기. 클릭하면 원본. `GET /api/workspace/file?path=` 가 워크스페이스 안 이미지만 서빙(지금은 업로드·다운로드만 있고 읽기 엔드포인트가 없다) |
| **웹 관찰 카드** | `read_file`·`screenshot` 결과에 같은 썸네일. "컨텍스트 밖"(살아 있는 수 상한·압축)이면 지금 규칙대로 흐리게 |
| **웹 재생(`_replay_record`)** | `images` 를 `push_user_message`/`observation` 페이로드에 실어 같은 카드 |
| **인스펙터** | 대화는 챗이 곧 모델 시점이므로 카드로 충분. 글 보기는 자리표시 줄 |
| **CLI(minimal)** | `🖼 shot.png (1280×720)` 한 줄 |
| **history/verbose** | `images` 필드 그대로, base64 없음 |

## 8. 단계

| 단계 | 내용 | 끝났다는 증거 |
|---|---|---|
| **P1 핵심 ✅ v10.59.0** | `ImageRef`·레코드 `images`·`ToolResult.images` / 두 provider `_expand_images` / 토큰 추정·`request_bytes` 분리 / 압축 자리표시 / `read_file` 이미지 분기 / `supports_vision`(models.json 키·프로브·마법사) / CLI `--image` / minimal 한 줄 / 재개(파일 없음·해시) | Qwen(openai)·Claude(anthropic) 양쪽에서 `run --image` 로 캡처 질문 정답, native_fc·json_fc 둘 다, 압축 뒤 재개 정상, 텍스트 모델에서 거절 문구 |
| **P2 웹** | 붙여넣기·끌어놓기·📎, `/api/input images`, 파일 서빙, 썸네일, 재생 | 헤드리스 크롬 테스트(`tests/browser/`) |
| **P3 확장** | browser `screenshot`, MCP 이미지, 살아 있는 수 상한·축소 기본값 실측 | A/B 재생 |

측정(P1 뒤): 과제 4종(캡처 속 오류 읽기 · 표 사진 → 숫자 · 두 화면 비교 · 도면 설명)
× 3회 × Qwen, 축소 단계별 판독률, 추정 토큰 대 서버 실측 오차.

## 9. 결정 (2026-10-21, 사용자)

> 아래 여덟 항목은 모두 결정됐다. 원안의 선택지는 기록으로 남긴다.

1. **진입 도구** — `read_file` 확장(추천) vs 별도 `view_image`. → **`read_file` 확장**.
2. **Pillow** — 선택 의존성 vs 필수 vs 축소 없음. → **필수** (설명 뒤 추천을 필수로 바꿨고 사용자가 택함).
3. **살아 있는 이미지 수** 기본값(후보 6) — 실측 뒤 조정. → **6, 0=무제한**.
4. **긴 변 상한** 기본값(후보 2048) — §6 실측 뒤. → **2048 후보 승인**, 축소값은 실측 뒤(P3).
5. **`supports_vision` 판정** — 프로브(1×1 PNG 보내 200 이면 참) vs models.json 수기만.
   프로브는 omlx 처럼 조용히 버리는 서버에서 거짓 양성이 날 수 있다 — user 메시지는
   실제로 봤으므로(§1) 프로브 질문을 "이 이미지의 색은?" 으로 하고 답을 검사하면 가린다.
   → **색을 묻는 프로브** (왼 green·오른 blue 두 색을 순서대로; `vision_support`, 프로세스당 한 번,
   사용자 엔트리는 파일에 안 씀 — 매번이 싫으면 `supports_vision` 을 적어 둔다).
6. **업로드 자리** `.agent-cli/tmp/uploads/`(추천) — 세션 재개 때도 남아야 하므로
   세션 디렉토리가 아니라 tmp. → **승인**.
7. **파일 서빙 범위** — 워크스페이스 안 이미지 전부(추천, 모델이 읽은 어느 그림이든
   카드에 보여야 하므로) vs `.agent-cli/tmp/` 만. → **워크스페이스 안 전부** (P2 에서 구현).
8. **CLI 플래그 이름** `--image`(반복 가능). → **`--image`** (`--vision` 안은 "플래그는 받는 값을
   이름으로" 라는 이유로 기능 이름에만 쓰기로).

## 10. 안 하는 것

- 모델 **출력** 이미지(생성) — 범위 밖.
- 영상 — 프레임 추출은 모델이 `shell ffmpeg` 로 하고 `read_file` 로 본다. 살아 있는 수
  상한(§4.1)이 그 때 창을 지킨다. 벤치 video 과제가 이걸로 풀리는지는 P3 뒤 확인.
- PDF 렌더 — 같은 길(`shell` 로 페이지를 PNG 로).
- 이미지 안의 글을 지시로 삼는 문제(prompt injection) — 글 관찰과 같은 규칙, 별도 방어 없음.

## 11. 출시 기록

| 버전 | 내용 |
|---|---|
| v10.59.0 | P1: `images.py`(참조·축소·공급자 경계 `expand`·프로브 이미지), `supports_vision` + `vision_support` 프로브, OpenAI `image_url`/Anthropic `image` 블록, `read_file` 이미지 분기, `ToolResult.images`, 관찰·질의 레코드 `images`, native_fc 는 `tool` 뒤 user 메시지, 토큰 추정·재조정, keep-N, `run --image`, minimal `🖼` 줄, 마법사 질문, 내장 모델 둘 `supports_vision: true`, Pillow 필수. 실측(§8 측정 계획)은 출시 뒤 과제. |
