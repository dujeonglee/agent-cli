"""압축 요약 호출의 글 (v10.123.0, docs/context-compaction/DESIGN.md §8.11).

두 모양이 같은 제목 일곱 줄을 쓴다 — 전사 모양(요약용 시스템 프롬프트 + 발췌 전사)과
경계 모양(본 호출 요청을 마지막 축출 메시지까지 자르고 뒤에 붙이는 지침). 제목은
여기 한 벌뿐이다: 두 벌이면 한쪽만 고쳐진다.

**경계 지침 문구는 측정한 그대로다** (35B·GLM 3사례 6/6 정상 요약, 도구 호출 0).
낱말을 바꾸면 측정 조건이 바뀐다 — 바꾸려면 다시 잰다.
"""

from __future__ import annotations

import re

#: 요약 제목 일곱 줄 — 전사 모양의 시스템 프롬프트와 경계 지침이 공유한다.
SUMMARY_HEADINGS = (
    "TASK — the user's original request(s) and intent.\n"
    "STATE — what is currently true: progress so far, what works.\n"
    "DONE — actions taken: tools used and exact file paths / "
    "symbols touched, and what changed.\n"
    "PENDING — work not yet finished and the next step that was "
    "about to run.\n"
    "DECISIONS — choices made and why, including alternatives "
    "rejected.\n"
    "FAILURES — approaches that failed or errors encountered, so "
    "they are not retried.\n"
    "FACTS — keep verbatim: paths, identifiers, commands, config "
    "values, signatures, error strings, and any user "
    "correction/preference.\n"
)

#: 제목 이름 — 경계 모양 응답이 요약인지 가리는 검사가 쓴다.
SUMMARY_HEADING_NAMES: tuple[str, ...] = tuple(
    line.split(" ", 1)[0] for line in SUMMARY_HEADINGS.splitlines()
)

#: 요약 프롬프트의 IMAGES 항목 — 요약하는 구간에 이미지가 있을 때만 FACTS 뒤에.
SUMMARY_IMAGES_LINE = (
    'IMAGES — one line per image in the transcript (marked "[image: …]"): its '
    "path, what it shows, and anything read from it that this summary does not "
    "already record. Many images of one kind (e.g. video frames) may share one "
    "line.\n"
)

#: 경계 줄 — 지침의 첫 줄. 이 줄 위가 압축되는 부분이다.
BOUNDARY_LINE = "==== end of compaction boundary ===="


def boundary_instruction(*, prior_summary: bool, images: bool) -> str:
    """경계 모양의 지침 — 본 호출 접두 뒤에 붙는다(측정한 문구 그대로).

    ``prior_summary``: 접두 머리에 "## Summary of earlier conversation" 메시지가
    있다 — 그것을 접어 넣으라는 줄을 싣는다. ``images``: 접두에 그림이 있다 —
    IMAGES 항목을 싣는다(그림은 접두 안에 본 호출과 같은 모양으로 이미 있다)."""
    text = (
        f"{BOUNDARY_LINE}\n"
        "[Harness compaction — not a task]\n"
        "The harness is compacting this conversation's context now. Everything "
        "above the boundary line is the part being compacted. This message is not "
        "from the user and is not a step of the task: do not continue the task, "
        "do not call any tool, do not answer the user. Reply with the summary "
        "text only.\n\n"
        "Summarise the conversation above the boundary line.\n"
    )
    if prior_summary:
        text += (
            'The "## Summary of earlier conversation" message near the top is the '
            "running summary so far. Fold it in: produce one updated summary, under "
            "the same headings, that covers it and the messages above the boundary "
            "line.\n"
        )
    text += (
        "\nWrite a plain-text summary under these headings (omit a heading if it "
        "has no content):\n\n"
        + SUMMARY_HEADINGS
        + (SUMMARY_IMAGES_LINE if images else "")
        + "\n"
        "Rules: use only information present in the conversation above the "
        "boundary line — do not invent or assume. Keep exact identifiers verbatim "
        "(paths, names, numbers, commands). Be concise; stay under 2000 tokens. "
        "Plain text only — no JSON, no code fences except to quote a short "
        "critical snippet."
    )
    return text


# 줄 머리의 제목 — 앞의 공백과 마크다운 장식(``## ``·``**``·``- ``·``> ``)은 허용한다.
_HEADING_AT_LINE_START = re.compile(
    r"^[\s#*_>\-]*(" + "|".join(SUMMARY_HEADING_NAMES) + r")\b", re.MULTILINE
)

#: 요약으로 받아들이는 최소 제목 수 — 과제를 이어 한 응답은 제목이 없다.
MIN_SUMMARY_HEADINGS = 2


def summary_heading_count(text: str) -> int:
    """``text`` 의 줄 머리에 나온 서로 다른 제목 수."""
    return len({m.group(1) for m in _HEADING_AT_LINE_START.finditer(text or "")})


def looks_like_summary(text: str) -> bool:
    """경계 모양 응답이 요약인가 — 제목 일곱 중 두 개 이상이 줄 머리에 있다."""
    return summary_heading_count(text) >= MIN_SUMMARY_HEADINGS
