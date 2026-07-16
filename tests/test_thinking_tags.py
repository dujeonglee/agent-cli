"""thinking_tags 단일 소스 — vocab·strip·정규식 계약 (Phase 2 선행 리팩토링).

층별 소비자(providers/openai ①②, WireFormat.strip_thinking stage 0,
capabilities vocab, json_fc ③ 정규식)가 전부 여기서 import 하므로,
이 모듈의 계약이 곧 4곳의 계약이다.
"""

from agent_cli.thinking_tags import (
    ORPHAN_THINK_TAG_RE,
    THINK_TAG_NAMES,
    TRAILING_THINK_TAG_RE,
    strip_think_blocks,
)


class TestVocab:
    def test_four_tags(self):
        assert THINK_TAG_NAMES == ("think", "thinking", "reasoning", "reflection")

    def test_consumers_share_vocab(self):
        # 드리프트 방지의 실체: 각 소비자가 같은 객체/값을 본다.
        from agent_cli.providers.capabilities import _THINKING_TAGS
        from agent_cli.providers.base import strip_think_blocks as provider_strip

        assert tuple(_THINKING_TAGS) == THINK_TAG_NAMES
        assert provider_strip is strip_think_blocks  # re-export 동일 객체


class TestStripThinkBlocks:
    def test_closed_block_removed_and_captured(self):
        cleaned, thinking = strip_think_blocks("<think>scratch</think>hello")
        assert cleaned == "hello"
        assert thinking == "scratch"

    def test_multiple_blocks_joined(self):
        cleaned, thinking = strip_think_blocks(
            "<think>a</think>mid<reasoning>b</reasoning>end"
        )
        assert cleaned == "midend"
        assert thinking == "a\n\nb"

    def test_attribute_bearing_tag(self):
        cleaned, thinking = strip_think_blocks('<think budget="1">x</think>ok')
        assert cleaned == "ok"
        assert thinking == "x"

    def test_unclosed_opener_consumes_to_eof(self):
        # max_tokens 를 추론 중 소진한 경우 — opener 이후 전부 추론.
        cleaned, thinking = strip_think_blocks("answer\n<think>ran out of tok")
        assert cleaned == "answer"
        assert "ran out of tok" in thinking

    def test_no_tags_passthrough(self):
        cleaned, thinking = strip_think_blocks("plain text, no tags")
        assert cleaned == "plain text, no tags"
        assert thinking == ""

    def test_orphan_closer_not_touched(self):
        # ③ 고아 closer 는 ①② 의 대상이 아니다 — 앵커드 처리(포맷 소유)로.
        # 문자열 값 안의 </think> 보존 계약(json_fc)이 이 성질에 기댄다.
        cleaned, thinking = strip_think_blocks('text with "</think>" inside')
        assert cleaned == 'text with "</think>" inside'
        assert thinking == ""

    def test_case_insensitive(self):
        cleaned, thinking = strip_think_blocks("<THINK>x</THINK>done")
        assert cleaned == "done"
        assert thinking == "x"


class TestOrphanRegexes:
    def test_orphan_matches_bare_tags(self):
        assert ORPHAN_THINK_TAG_RE.sub("", "a</thinking>b<think>c") == "abc"

    def test_trailing_anchored_only(self):
        # 끝에 앵커 — 중간의 태그는 손대지 않는다.
        assert TRAILING_THINK_TAG_RE.sub("", "[{}]</think>") == "[{}]"
        assert TRAILING_THINK_TAG_RE.sub("", "a</think>b") == "a</think>b"


class TestWireFormatStage0:
    def test_abc_helper_contract(self):
        # (cleaned, thinking|None) — thinking 없으면 None (react 계약).
        from agent_cli.wire_formats import WireFormat

        assert WireFormat.strip_thinking("plain") == ("plain", None)
        cleaned, thinking = WireFormat.strip_thinking("<think>t</think>x")
        assert cleaned == "x"
        assert thinking == "t"

    def test_json_fc_stage0_strips_leading_block(self):
        # 종전 갭: json_fc 는 ①② 자체 처리가 없어 provider 미경유 경로에서
        # 선두 think 블록이 thought 로 오염됐다 — stage 0 helper 로 봉합.
        from agent_cli.wire_formats import get as get_wf

        wf = get_wf("json_fc")
        turn = wf.parse_turn(
            "<think>internal scratch</think>\n"
            "## Thought\nreal thought\n\n"
            '## Action\n[{"action": "read_file", "path": "a.py"}]'
        )
        assert turn.thinking == "internal scratch"
        assert turn.thought == "real thought"
        assert len(turn.ops) == 1
        assert turn.ops[0].action == "read_file"

    def test_json_fc_unclosed_opener_recovered(self):
        from agent_cli.wire_formats import get as get_wf

        wf = get_wf("json_fc")
        turn = wf.parse_turn(
            "## Thought\nt\n\n"
            '## Action\n[{"action": "complete", "result": "done"}]\n'
            "<think>tail reasoning cut off"
        )
        assert turn.ops and turn.ops[0].action == "complete"
        assert "tail reasoning cut off" in (turn.thinking or "")

    def test_react_stage0_still_captures_thinking(self):
        from agent_cli.wire_formats import get as get_wf

        wf = get_wf("react")
        pa = wf.parse(
            "<think>scratch</think>"
            '{"thought": "t", "action": "complete", "action_input": {"result": "r"}}'
        )
        assert pa.thinking is not None and "scratch" in pa.thinking
        assert pa.action == "complete"
