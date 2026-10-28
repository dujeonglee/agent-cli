# 이미지 입력 (vision) — 설계

> 상태: **P1 출시 (v10.59.0)** · **P2 웹 출시 (v10.60.0)** · **P3 출시 (v10.61.0, 2026-10-21)** — 두 공급자·`read_file`·CLI `--image`·능력 프로브·토큰·살아 있는 수 상한·재개 + 웹 붙여넣기·끌어놓기·📎·썸네일·재생 + browser `screenshot`·MCP 이미지·에이전트 채널 이미지. 남은 것은 §8 의 실측(축소 기본값·keep-N).

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

~~**살아 있는 이미지 수 상한(`AGENT_CLI_IMAGE_KEEP`, 기본 6; `images.keep_recent`).**~~ —
**v10.77.0 에서 삭제(§12.1).** 이미지는 붙은 레코드와 함께 압축이 내보낼 때만 사라진다.

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
| **사용자 — 웹** | 입력창에 붙여넣기(`paste` 의 `clipboardData.files`) · 끌어다 놓기 · 📎 버튼 → `POST /api/images/upload?name=<name>`(서버가 `.agent-cli/tmp/uploads/<epoch>-<name>` 에 두고 매직 바이트를 검사, `ImageRef` 반환) → 칩 → `POST /api/input {"kind":"chat","content":…,"images":[path]}`(서버가 참조를 다시 만든다). 이미지만 있는 메시지도 된다. 런 시작 에코·턴 경계 주입 둘 다 `images` 를 싣는다 | 질의 | P2 ✅ v10.60.0 |
| **사용자 — 웹, 에이전트 채널** | `POST /api/agent/{key}/input {"content":…,"images":[path]}` — 같은 `image_refs` 검증 → `registry.request(images=)` → inbox 항목 `images` → `_run_message(query_images=)` → `run_subagent_message(query_images=)` → 런의 첫 user 레코드. 🤝 창의 `agent_msg`(in) 와 `conversation.jsonl` 에도 실려 ⏳ 대기 줄·런 머리 썸네일, resume 재생. 답(`answer_id`)에는 안 붙는다(400) | 질의 | P3 ✅ v10.61.0 |
| **모델 — `read_file`** | 경로의 매직 바이트가 이미지면 글 대신 `images=[ref]`, `output` 첫 줄은 자리표시 `[image: a.png (1280×720)]`, 둘째 줄 `image/png, 184,302 bytes — attached` | 관찰 | P1 ✅ |
| **모델 — `browser`** | `mode: "screenshot"` {`full_page?`} → `BrowserSession.screenshot(path)`(`page.screenshot`) → `.agent-cli/tmp/screenshots/<epoch>-shot.png` + `images=[ref]`; 출력은 `read_file` 과 같은 모양(자리표시 줄 + 파일 정보) | 관찰 | P3 ✅ v10.61.0 |
| **모델 — fetch** | 이미 `downloads/` 에 저장 — 그다음 `read_file` 로 본다. 변경 없음 | — | — |
| **MCP 도구** | `adapter._extract_mcp_result` 가 `type=="image"` 조각을 base64 디코드해 `.agent-cli/tmp/mcp/<epoch>-<server>-<tool>-<n>.<ext>` 로 두고 자리표시 줄 + `images`; 깨진 base64 는 사유 한 줄, 못 보는 형식(svg 등)은 저장만. 종전엔 base64 가 글로 들어갔다 | 관찰 | P3 ✅ v10.61.0 |

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
- 축소는 Pillow — **선택 의존성 `agent-cli[image]`** (결정 2026-10-21 저녁, v10.61.1; 같은 날
  아침의 "필수" 결정을 뒤집음 — 사내 pip 미러에 Pillow 가 없는 배포가 실제로 있었다).
  크기 읽기는 `image_size`(PNG·GIF·JPEG·WebP 헤더, 표준 라이브러리), 프로브 이미지는
  상수 PNG 라 **이미지 기능 자체는 의존성 0**. Pillow 가 없을 때 빠지는 것은 보내기 전
  축소·JPEG 재인코딩뿐이고(§8.1 실측: 축소는 정확도를 올리지 않고 토큰만 아낀다), 바이트
  상한(5MB)을 넘는 이미지만 "too large — downscale it first; pip install pillow …" 사유로
  빠진다. 자체 축소기(PNG 전용 순수 파이썬, 수백 줄·FHD 수 초)와 라이브러리 동봉(C 확장이라
  플랫폼별 휠 — 불가)은 검토 뒤 기각.
- 기본값은 §1 실측(1280×720 에서 60px 오독, FHD 정확)이 축소에 불리한 신호라
  **실측 뒤** 정한다: 같은 화면을 원본·2048·1568·1024 로 보내 글자 판독률을 잰다.

## 7. 표면

| 표면 | 무엇 |
|---|---|
| **대기 줄·런 머리 (v10.61.3)** | main 대기열(`⏳[닉]`)·에이전트 채널 ⏳ 줄·런 머리 `← 본문` 의 요약 칸 끝에 28px 작은 썸네일 — 접힌 채로도 이미지가 있다는 사실이 보인다(사용자 실측: 대기열에 아무것도 안 보였다). 펼치면 160px |
| **웹 사용자 카드** | 썸네일(최대 높이 160px) + 파일명·크기. 클릭하면 원본(새 탭). `GET /api/workspace/file?path=` 가 워크스페이스 안 **이미지만** 서빙(매직 바이트; 글 파일은 400). 본문의 `[image: …]` 자리표시 줄은 썸네일이 대신하므로 뗀다 ✅ |
| **웹 관찰 카드** | `read_file`·`screenshot` 결과에 같은 썸네일 — 접힌 행 안이라 펼쳐야 로드된다(lazy). "컨텍스트 밖"(살아 있는 수 상한·압축)이면 지금 규칙대로 흐리게 ✅ |
| **웹 재생(`_replay_record`)** | `images` 를 `push_user_message`/`observation` 페이로드에 실어 같은 카드 ✅ |
| **인스펙터** | 대화는 챗이 곧 모델 시점이므로 카드로 충분. 글 보기는 자리표시 줄 |
| **CLI(minimal)** | `🖼 shot.png (1280×720)` 한 줄 |
| **history/verbose** | `images` 필드 그대로, base64 없음 |

## 8. 단계

| 단계 | 내용 | 끝났다는 증거 |
|---|---|---|
| **P1 핵심 ✅ v10.59.0** | `ImageRef`·레코드 `images`·`ToolResult.images` / 두 provider `_expand_images` / 토큰 추정·`request_bytes` 분리 / 압축 자리표시 / `read_file` 이미지 분기 / `supports_vision`(models.json 키·프로브·마법사) / CLI `--image` / minimal 한 줄 / 재개(파일 없음·해시) | Qwen(openai)·Claude(anthropic) 양쪽에서 `run --image` 로 캡처 질문 정답, native_fc·json_fc 둘 다, 압축 뒤 재개 정상, 텍스트 모델에서 거절 문구 |
| **P2 웹 ✅ v10.60.0** | 붙여넣기·끌어놓기·📎, `/api/images/upload`·`/api/images/support`, `/api/input images`, `/api/workspace/file` 서빙, 썸네일(사용자·관찰), 재생, 주입 메시지 이미지, 못 보는 모델 안내 | 헤드리스 크롬 테스트 `tests/browser/test_image_attach.py` 3건 통과(첨부→큐 왕복, 썸네일 실제 로드, 📎 숨김) |
| **P3 확장 ✅ v10.61.0** | browser `screenshot`, MCP 이미지, 에이전트 채널 이미지 | 유닛(가짜 페이지·mcp.types·가짜 러너) + 실브라우저 `test_screenshot_writes_a_real_png` |
| **P4 실측** | 살아 있는 수 상한·축소 기본값 실측(아래 측정 계획) | A/B 재생 |

측정(P1 뒤): 과제 4종(캡처 속 오류 읽기 · 표 사진 → 숫자 · 두 화면 비교 · 도면 설명)
× 3회 × Qwen, 축소 단계별 판독률, 추정 토큰 대 서버 실측 오차.

### 8.1 축소 단계별 판독률 — 실측 (2026-10-21, omlx Qwen3.6-35B-A3B-8bit, thinking off, 3회)

합성 1920×1080 화면 4종(Menlo 12~14px 글자): 터미널 오류(코드 `E4471`·`router.py:218`),
표(Q3 Revenue), 두 설정 패널 비교(cache enabled/disabled), 상자 도면(화살표 라벨 `retry 3x`).
공급자 경계와 같은 축소 코드(`images._encode_bytes`)로 긴 변을 바꿔 보냈다. 스크립트
scratchpad `vision/measure/measure.py`.

| 긴 변 | 크기 | prompt 토큰 | 오류 | 표 | 비교 | 도면 |
|---|---|---|---|---|---|---|
| 원본 | 1920×1080 | 2,071 | 3/3 | 3/3 | 3/3 | 3/3 |
| 1568 | 1568×882 | 1,403 | 3/3 | 3/3 | 3/3 | 3/3 |
| 1280 | 1280×720 | 911 | 3/3 | 3/3 | 3/3 | 3/3 |
| 1024 | 1024×576 | 607 | **1/3** | 3/3 | 3/3 | 3/3 |
| 768 | 768×432 | 367 | **0/3** | 3/3 | 3/3 | 3/3 |

- 토큰은 픽셀에 정확히 비례(≈ 1,000 px/token, §4.3 상수 그대로). 1568 은 원본 대비 −32%,
  1280 은 −56%.
- 깨지는 것은 **작은 글자**뿐이다: 1024 부터 `E4471` 을 `4471`·`E4871`·`4041` 로 읽는다.
  표·비교·도면은 768 에서도 정확하다.
- 기본값 2048 은 FHD 캡처를 원본 그대로 보낸다. 1568 로 내리면 이 과제들에서는 손실 없이
  토큰 1/3 을 아낀다. 1280 도 여기서는 손실이 없지만 §1 의 0층 실측(다른 캡처, 60px 글자
  오독)과 함께 보면 여유가 없다. **결정은 사용자 몫** — 바꾸려면 `IMAGE_MAX_EDGE_DEFAULT`
  한 상수다.
- keep-N(6)은 비용만 셈했다: 1280×720 여섯 장 ≈ 5.5K 토큰/요청, FHD 원본 여섯 장 ≈ 12.4K.
  회상 손실 측정은 다턴 과제가 필요해 하지 않았다.

## 9. 결정 (2026-10-21, 사용자)

> 아래 여덟 항목은 모두 결정됐다. 원안의 선택지는 기록으로 남긴다.

1. **진입 도구** — `read_file` 확장(추천) vs 별도 `view_image`. → **`read_file` 확장**.
2. **Pillow** — 선택 의존성 vs 필수 vs 축소 없음. → **필수** (설명 뒤 추천을 필수로 바꿨고 사용자가 택함). → **선택으로 되돌림 (같은 날, v10.61.1)**: 사내 미러에 Pillow 가 없어 설치가 막혔다. 크기·프로브를 의존성 0 으로 바꿔 없을 때 빠지는 건 축소 하나.
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
- 영상 — 프레임 추출은 모델이 `shell ffmpeg` 로 하고 `view_image` 로 본다(v10.77.0 전엔
  `read_file`). 창은 압축과 턴 예산 가드(§12.1)가 지킨다.
- PDF 렌더 — 같은 길(`shell` 로 페이지를 PNG 로).
- 이미지 안의 글을 지시로 삼는 문제(prompt injection) — 글 관찰과 같은 규칙, 별도 방어 없음.

## 11. 출시 기록

| 버전 | 내용 |
|---|---|
| v10.77.0 | §12: `view_image` 도구(read_file 이미지 분기 이전), 도구를 바꾸는 층 `loop/reroute.py`(read_file 이미지 → view_image, 레코드에 바뀐 호출 + `rewritten_from`), 장수 상한 `keep_recent`·`AGENT_CLI_IMAGE_KEEP` 삭제(압축만), 요약 IMAGES 항목·`## Images viewed in earlier turns` 블록(`image_list`), read_file 의 텍스트 아님 한 줄. |
| v10.68.0 | 프로브 결과 영속: `vision_support` 가 판정(답 수신 또는 4xx)이면 `config.fill_model_entry` 로 엔트리의 **빠진** `supports_vision` 만 채운다. 연결 실패·5xx 는 프로세스 한정. 동기: v10.59.0 이전 엔트리 셋이 harbor trial·보드 방마다 다시 프로브했다. |
| v10.61.3 | 대기 줄·런 머리의 작은 썸네일(`miniThumbs`) — 사용자 검토 1차. |
| v10.61.1 | Pillow 선택 의존성(`[image]`): `image_size` 헤더 파싱, 프로브 PNG 상수, `has_pillow`, `ImageTooLarge`(5MB 초과만 거절). 사내 미러에 Pillow 가 없는 배포 실측. |
| v10.61.0 | P3: browser `screenshot` 모드(`BrowserSession.screenshot`, `SCREENSHOT_DIR`) · MCP 이미지 조각 → 파일 + 참조(`_save_mcp_image`, `MCP_IMAGE_DIR`, `IMAGE_EXT`) · 에이전트 채널 이미지(`/api/agent/{key}/input images` → `request(images=)` → inbox → `run_subagent_message(query_images=)`; `agent_msg`·`conversation.jsonl` 에 `images`, 프런트 ⏳ 줄·런 머리 썸네일) · `fresh_upload_path(subdir=)`. 실측(축소·keep-N)은 P4 로 남긴다. |
| v10.60.0 | P2 웹: `POST /api/images/upload`(공용 스트리밍 `_stream_body_to`, `images.fresh_upload_path`, 매직 바이트 검사) · `GET /api/images/support`(`vision_known` — 프로브 없음) · `GET /api/workspace/file`(이미지만) · `/api/input` chat `images: [path]` → `WebServer.image_refs` 재검증 · 큐→`RunRequest`→런 시작 에코와 턴 경계 주입(`_inject_queued_messages`) 둘 다 `images` · 렌더러 ABC `observation(images=)`/`push_user_message(images=)` · web 페이로드·재생 · 프런트 📎/붙여넣기/끌어놓기 → 칩 → 보내기, 사용자·관찰 썸네일, 못 보는 모델이면 📎 숨김(+ 웹 런 시작 `status` 한 줄) · minimal 주입 에코 `🖼` 줄. 에이전트 채널 이미지는 P3 로 미룸(메일 경로). |
| v10.59.0 | P1: `images.py`(참조·축소·공급자 경계 `expand`·프로브 이미지), `supports_vision` + `vision_support` 프로브, OpenAI `image_url`/Anthropic `image` 블록, `read_file` 이미지 분기, `ToolResult.images`, 관찰·질의 레코드 `images`, native_fc 는 `tool` 뒤 user 메시지, 토큰 추정·재조정, keep-N, `run --image`, minimal `🖼` 줄, 마법사 질문, 내장 모델 둘 `supports_vision: true`, Pillow 필수. 실측(§8 측정 계획)은 출시 뒤 과제. |

## 12. view_image · 도구를 바꾸는 층 · 압축 (v10.77.0, 2026-10-28)

동기: 9과제 짝 벤치(2026-10-27) GLM financial r2 — JPG 11장을 차례로 읽자 6장 상한이 앞의
5장을 말없이 뗐고, 모델은 빠진 그림을 다시 읽었다. 다시 읽을 때마다 다른 그림이 밀려나 15턴을
돌다 시간 초과(같은 그림 다시 읽기 25회). 11장 × 2.2K = 24K 토큰, 262K 창의 9% 라 창에는 여유가
있었다. 검토(사용자와, 개정안 1~5판)에서 모델이 직접 내리는 hide·20% 예산 방어·꼬리 `## Images`
목록까지 갔다가, **숨기기도 대화 중간을 바꿔 KV 캐시를 깨므로** 압축에 맡기기로 정했다.

### 12.1 이미지가 빠지는 길은 압축 하나

- `keep_recent`·`image_keep`·`IMAGE_KEEP_DEFAULT`·`AGENT_CLI_IMAGE_KEEP` 삭제(호환층 없음). 대화
  중간의 참조를 떼면 그 지점부터 끝까지 다시 prefill 한다(GLM 1.3K tok/s 면 뒤 50K 에 40초).
  압축은 원래 앞부분 전체를 바꾸므로 추가 비용이 없다.
- 한 턴에 창을 넘는 이미지는 압축이 못 막는다(지금 턴은 못 내보냄) — 기존 `ToolBridge.observe`
  가 이미 이미지 토큰을 op 캡·턴 예산에 넣는다(v10.59.0). 새 장치 없음.
- 같은 그림을 다시 보면 새 결과에도 붙고 앞의 것도 그대로다 — 앞의 것을 떼면 캐시가 깨진다.

### 12.2 `view_image` 와 도구를 바꾸는 층

- `view_image(path)` — read_file 의 이미지 분기(`_read_image`)를 옮긴 도구. 결과는 같은 두 줄
  (자리표시 + `mime, bytes — the image is attached to this result.`). `requires_handler="vision"`:
  바인딩이 `supports_vision: false` 면 광고하지 않고, 모르면(프로브 전) 광고한다.
- **구제하지 않는다**(사용자 결정): 목록 `path` 는 `PATH_NOT_ONE` 오류("call view_image once per
  image"). 관측되면 그때 구제한다.
- read_file 은 텍스트만 — 설명의 이미지 문장 삭제, 텍스트가 아닌 파일은 `read_file: X is not a
  text file`(종전 `'utf-8' codec can't decode …`).
- **층** `agent_cli/loop/reroute.py`: 결과가 똑같다고 보장될 때만 도구를 바꾼다. 규칙 하나 —
  read_file(매직 바이트가 이미지) → view_image, 루프에 view_image 가 있을 때만(지난 벤치에서
  read_file 로 이미지 53회 — 관측된 경우). 이미지에 뜻이 없는 `line_start`·`search`·`stat` 은
  버린다(바뀐 호출이 스키마에 맞게). `_handle_text_path` 가 파싱 직후 부르고, 바꾼 턴의 레코드는
  ops shape 로 만든다 — **컨텍스트·history 에 바뀐 호출이 남는다**(사용자 결정: 모델의 자기
  기록이 곧 예시). 원래 호출은 `Op.rewritten_from` → 레코드의 `rewritten_from`(렌더러는
  `action`·`action_input` 만 읽어 모델에게 안 그린다). 결과에 알림 줄은 없다. turns.jsonl
  primitives 에 `reroute:read_file>view_image`.
- 범위: 도구에서 도구로만. 인자 모양 구제는 각 도구 안. `shell cat` → read_file 은 조건부 후보,
  `shell python` → python 은 뜻이 달라(REPL·인터프리터) 넣지 않는다.

### 12.3 압축할 때 이미지

요약기는 같은 모델이고 이미지를 떼고 받는다(`strip_images`) — 사라지는 것은 아직 글로 옮기지 않은
그림의 내용과 "무엇을 봤나" 의 기억.

- (a) 요약 프롬프트 FACTS 뒤 `IMAGES — for each image in the transcript (marked "[image: …]"): its
  path, and what was read from it that this summary does not already record.` — 입력에 자리표시가
  있을 때만(`images.mentions_image`). 없으면 프롬프트가 종전과 바이트 같다. 개정안의 "a line
  starting" 은 요약 입력이 `[view_image] → [image: …]` 한 줄로 풀려 사실이 아니라 "marked" 로.
- (b) 하니스가 내보낸 레코드의 `images` 를 모아(`_evicted_images`, `경로 (W×H)`, 경로 중복 없음)
  요약·`## Files touched` 뒤에 `## Images viewed in earlier turns — their pictures left with the
  summarized turns; viewing one again shows it` 블록. `compaction.json` 의 `image_list` 로 resume
  에도 남는다. 압축 때만 바뀐다 — 다시 본 그림도 빼지 않는다(빼면 앞자리 블록이 바뀌어 대화 전체를
  다시 prefill). 도구 이름을 쓰지 않는 것은 이 ctx 의 루프가 view_image 를 가졌는지 ctx 가 모르기
  때문(개정안의 "view_image shows one again" 에서 바꿈). 못 본다고 알려진 모델이면 싣지 않는다.
- (c) 요약기에 그림 자체를 싣는 안은 a·b 의 측정 뒤 변형으로.

### 12.4 측정과 회귀 판정

10.76.1 기준과 짝, GLM·35B, financial·video 교차 반복. 먼저 셀 것: 층이 바꾼 수와 view_image 를
직접 부른 수, 같은 이미지 다시 보기 수, view_image 인자 오류 종류별 수, 압축 횟수·첫 압축 턴·압축
뒤 다시 보기 수·요약의 IMAGES 항목, 그림에서 읽을 값을 틀린 수, 이미지 턴의 TTFT·`cached_tokens`,
그다음 통과 수·턴 수. **회귀**(어느 한 모델이라도 두 짝 모두에서 두 과제 통과 수가 기준보다 적거나
다시 보기·압축이 눈에 띄게 늘면)면 이미지를 read_file 로 되돌리고 층은 규칙만 비운 채 남긴다.

