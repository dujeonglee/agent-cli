"""`view_image` · 도구를 바꾸는 층 · 압축과 이미지 (v10.77.0, docs/vision/DESIGN.md §12).

- 이미지는 read_file 이 아니라 view_image 로 본다. 못 본다고 알려진 모델에는
  광고하지 않는다.
- 모델이 read_file 로 이미지를 열면 층이 view_image 호출로 바꾼다 — 기록·
  컨텍스트에는 바뀐 호출, 원래 호출은 ``rewritten_from`` 에만(모델에게는 안 그림).
- 이미지는 압축이 내보낼 때만 빠진다. 요약 프롬프트에 IMAGES 항목(이미지가 있을
  때만), 요약 뒤에 결정적 목록 블록.
"""

from __future__ import annotations

import json

import pytest

from agent_cli.context.manager import ContextManager, images_block_head
from agent_cli.dialects import get as get_dialect
from agent_cli.dialects.base import Op
from agent_cli.loop.reroute import reroute
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools import TOOLS
from agent_cli.tools.base import RunContext
from agent_cli.tools.view_image import PATH_NOT_ONE
from tests.binding_support import make_binding
from tests.test_format_nudge_record import _dispatcher


def _png(path, size=(80, 40)):
    from PIL import Image

    Image.new("RGB", size, (0, 0, 255)).save(path, format="PNG")
    return path


@pytest.fixture
def shot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _png(tmp_path / "shot.png")
    return "shot.png"


# ── 도구 ────────────────────────────────────────────────────────────


class TestTool:
    def test_view_attaches_the_image(self, shot):
        r = TOOLS["view_image"].run({"path": shot})
        assert r.success
        assert r.output.splitlines() == [
            "[image: shot.png (80×40)]",
            "image/png, " + f"{r.images[0]['bytes']:,}"
            " bytes — the image is attached to this result.",
        ]
        assert r.images[0]["path"] == "shot.png"

    def test_text_file_says_not_an_image_and_points_to_read_file(self, tmp_path):
        p = tmp_path / "notes.txt"
        p.write_text("hi")
        tools = frozenset({"read_file", "view_image"})
        r = TOOLS["view_image"].run(
            {"path": str(p)}, ctx=RunContext(tools_available=tools)
        )
        assert r.error == (
            f"view_image: {p} is not a PNG, JPEG, GIF or WebP image"
            " — read text files with read_file"
        )
        r = TOOLS["view_image"].run(
            {"path": str(p)}, ctx=RunContext(tools_available=frozenset())
        )
        assert r.error.endswith("WebP image")

    def test_missing_file(self, tmp_path):
        r = TOOLS["view_image"].run({"path": str(tmp_path / "x.png")})
        assert r.error.startswith("view_image failed: [Errno 2]")

    def test_a_list_of_paths_is_not_rescued(self, shot):
        """구제하지 않는다 — 관측되면 그때 (2026-10-28 결정)."""
        r = TOOLS["view_image"].run({"path": [shot, shot]})
        assert not r.success and r.error == PATH_NOT_ONE

    def test_schema_and_order(self):
        t = TOOLS["view_image"]
        assert t.parameters["required"] == ["path"]
        assert list(t.parameters["properties"]) == ["path"]
        assert t.requires_handler == "vision"
        order = list(TOOLS)
        assert order.index("code_index") + 1 == order.index("view_image")


class TestAdvertised:
    def _tools(self, vision):
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop
        from tests.loop_ports import TEST_PORTS

        loop = AgentLoop(
            ports=TEST_PORTS,
            query="Q",
            provider=MagicMock(),
            capabilities=ModelCapabilities(
                context_window=32768,
                max_output_tokens=4096,
                supports_thinking=False,
                supports_vision=vision,
            ),
            model="m",
            dialect=get_dialect("json_fc"),
        )
        return loop._llm.cfg.tools_list

    def test_known_blind_model_does_not_get_it(self):
        assert "view_image" not in self._tools(False)

    @pytest.mark.parametrize("vision", [True, None])
    def test_seeing_or_unknown_model_gets_it(self, vision):
        assert "view_image" in self._tools(vision)


# ── 도구를 바꾸는 층 ────────────────────────────────────────────────


class TestReroute:
    def test_read_file_of_an_image_becomes_view_image(self, shot):
        op = Op(action="read_file", action_input={"path": shot, "line_start": 3})
        assert reroute([op], ["read_file", "view_image"]) == [
            "reroute:read_file>view_image"
        ]
        assert op.action == "view_image"
        # 이미지에 뜻이 없는 인자는 버린다 — 바뀐 호출은 스키마에 맞는다
        assert op.action_input == {"path": shot}
        assert op.rewritten_from == {
            "action": "read_file",
            "action_input": {"path": shot, "line_start": 3},
        }

    def test_text_file_and_loops_without_view_image_are_left_alone(
        self, shot, tmp_path
    ):
        (tmp_path / "a.txt").write_text("x")
        text = Op(action="read_file", action_input={"path": "a.txt"})
        image = Op(action="read_file", action_input={"path": shot})
        assert reroute([text], ["read_file", "view_image"]) == []
        assert reroute([image], ["read_file"]) == []
        assert text.action == image.action == "read_file"
        assert text.rewritten_from is None and image.rewritten_from is None

    @pytest.mark.parametrize("dialect", ["native_fc", "json_fc"])
    def test_record_and_context_carry_the_rewritten_call(self, shot, tmp_path, dialect):
        d, ctx, _ = _dispatcher(tmp_path, dialect)
        d.cfg.tools_list.extend(["read_file", "view_image"])
        if dialect == "native_fc":
            d._handle_text_path(
                "",
                server_ops=[Op(action="read_file", action_input={"path": shot})],
            )
        else:
            d._handle_text_path(
                json.dumps([{"action": "read_file", "path": shot}]),
            )
        recs = [json.loads(x) for x in ctx.history_path.read_text().splitlines()]
        asst = next(r for r in recs if r.get("role") == "assistant")
        assert asst["ops"] == [
            {
                "action": "view_image",
                "action_input": {"path": shot},
                "rewritten_from": {
                    "action": "read_file",
                    "action_input": {"path": shot},
                },
            }
        ]
        obs = next(r for r in recs if r.get("role") == "user" and r.get("images"))
        assert obs["images"][0]["path"] == shot
        # 결과에 알림 줄은 없다
        assert "read_file" not in obs["content"]
        # 모델이 보는 기록: 바뀐 호출만, rewritten_from 은 그리지 않는다
        wire = json.dumps(ctx.get_messages(), ensure_ascii=False)
        assert "view_image" in wire and "rewritten_from" not in wire
        assert '"read_file"' not in wire
        assert "reroute:read_file>view_image" in d.last_outcome["primitives"]


# ── 압축과 이미지 ──────────────────────────────────────────────────


def _ref(path, w=1240, h=1754):
    return {"path": path, "mime": "image/jpeg", "width": w, "height": h, "bytes": 9}


def _ctx(tmp_path, *, vision=None, seen=None):
    caps = ModelCapabilities(
        context_window=100_000,
        max_output_tokens=4096,
        supports_thinking=False,
        supports_vision=vision,
    )
    ctx = ContextManager(
        tmp_path / "s",
        max_context_tokens=100_000,
        binding=make_binding(capabilities=caps),
    )
    seen = seen if seen is not None else []

    def summarise(messages):
        seen.append(messages)
        return "SUMMARY"

    ctx.set_compactor(summarise)
    ctx.add({"role": "system", "content": "sys"})
    ctx.set_turn(0)
    ctx.add({"role": "user", "content": "read the invoices"})
    for n, name in enumerate(["a.jpg", "b.jpg", "a.jpg"], 1):
        ctx.set_turn(n)
        ctx.add(
            {
                "role": "assistant",
                "ops": [{"action": "view_image", "action_input": {"path": name}}],
            }
        )
        ctx.add(
            {
                "role": "user",
                "tool": "view_image",
                "success": True,
                "content": f"[image: {name} (1240×1754)]\nimage/jpeg, 9 bytes",
                "images": [_ref(name)],
            }
        )
    for n in range(4, 9):
        ctx.set_turn(n)
        ctx.add({"role": "assistant", "ops": [{"action": "shell", "action_input": {}}]})
        ctx.add({"role": "user", "tool": "shell", "content": f"out{n} " * 60})
    return ctx


def _block(ctx):
    return [
        m["content"]
        for m in ctx.get_messages()
        if str(m.get("content", "")).startswith("## Images viewed")
    ]


class TestCompaction:
    def test_evicted_images_are_listed_once_after_the_summary(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert _block(ctx) == []
        ctx.compact_now()
        assert ctx.image_list == ["a.jpg (1240×1754)", "b.jpg (1240×1754)"]
        assert _block(ctx) == [
            f"{images_block_head()}\n\n- a.jpg (1240×1754)\n- b.jpg (1240×1754)"
        ]
        heads = [str(m.get("content", "")) for m in ctx.get_messages()]
        summary = next(i for i, c in enumerate(heads) if c.startswith("## Summary"))
        images = next(i for i, c in enumerate(heads) if c.startswith("## Images"))
        assert summary < images

    def test_head_names_the_viewer_only_when_the_loop_has_it(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.compact_now()
        # 루프가 알려 주기 전(기본): 다시 보는 길을 말하지 않는다
        assert _block(ctx)[0].split("\n")[0] == (
            "## Images viewed in earlier turns — their pictures left with the "
            "summarized turns"
        )
        ctx.set_image_viewer("view_image")
        assert _block(ctx)[0].startswith(
            "## Images viewed in earlier turns — their pictures left with the "
            "summarized turns; viewing one again with view_image shows it\n"
        )
        ctx.set_image_viewer(None)
        assert "again" not in _block(ctx)[0]

    def test_loop_tells_the_context_its_viewer(self, tmp_path):
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop
        from tests.loop_ports import TEST_PORTS

        for vision, expect in ((True, "view_image"), (False, None)):
            ctx = ContextManager(tmp_path / f"s{vision}", max_context_tokens=100_000)
            AgentLoop(
                ports=TEST_PORTS,
                query="Q",
                provider=MagicMock(),
                capabilities=ModelCapabilities(
                    context_window=32768,
                    max_output_tokens=4096,
                    supports_thinking=False,
                    supports_vision=vision,
                ),
                model="m",
                dialect=get_dialect("json_fc"),
                ctx=ctx,
            )
            assert ctx._image_viewer == expect

    def test_summariser_sees_the_evicted_pictures_once_each(self, tmp_path):
        seen: list = []
        ctx = _ctx(tmp_path, seen=seen)
        ctx.compact_now()
        (msg,) = seen[-1]
        # a.jpg 를 두 번 봤어도 같은 그림은 한 번
        assert [r["path"] for r in msg["images"]] == ["a.jpg", "b.jpg"]
        assert msg["content"].endswith(
            "The images from this transcript are attached below, in this order:"
            "\n1. a.jpg\n2. b.jpg"
        )

    def test_blind_model_summariser_gets_no_pictures(self, tmp_path):
        seen: list = []
        ctx = _ctx(tmp_path, vision=False, seen=seen)
        ctx.compact_now()
        (msg,) = seen[-1]
        assert "images" not in msg and "attached below" not in msg["content"]

    def test_list_survives_resume(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.compact_now()
        again = ContextManager(tmp_path / "s", max_context_tokens=100_000, resume=True)
        assert again.image_list == ctx.image_list

    def test_known_blind_model_gets_no_list(self, tmp_path):
        ctx = _ctx(tmp_path, vision=False)
        ctx.compact_now()
        assert ctx.image_list and _block(ctx) == []

    def test_summary_prompt_asks_for_images_only_when_there_were_some(self):
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop
        from agent_cli.loop.llm import _SUMMARY_IMAGES_LINE
        from tests.loop_ports import TEST_PORTS

        provider = MagicMock()
        provider.call.return_value = MagicMock(content="S")
        loop = AgentLoop(
            ports=TEST_PORTS,
            query="Q",
            provider=provider,
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            dialect=get_dialect("json_fc"),
        )
        with_image = [
            {"role": "user", "content": "[view_image] → [image: a.png (2×2)]"}
        ]
        loop._llm._llm_compact_summarize(with_image)
        assert _SUMMARY_IMAGES_LINE in provider.call.call_args.kwargs["system"]
        assert "what it shows" in _SUMMARY_IMAGES_LINE
        # 요약기에 그림이 그대로 간다 (공급자 경계가 펼친다)
        refs = [{"path": "a.png", "width": 2, "height": 2}]
        loop._llm._llm_compact_summarize([{**with_image[0], "images": refs}])
        assert provider.call.call_args.kwargs["messages"][0]["images"] == refs
        loop._llm._llm_compact_summarize([{"role": "user", "content": "plain"}])
        system = provider.call.call_args.kwargs["system"]
        assert "IMAGES" not in system
        assert "correction/preference.\n\nRules:" in system  # 종전과 바이트 같다
