"""이미지 입력 (v10.59.0, docs/vision/DESIGN.md) — ``agent_cli.images`` 와 그
접점(툴 브리지의 캡·루프의 사용자 메시지·입력 큐)."""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import pytest
from PIL import Image

from agent_cli import images
from agent_cli.images import ImageRef


def make_png(path: Path, size=(64, 32), color=(255, 0, 0)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


@pytest.fixture(autouse=True)
def _clear_cache():
    images._encode_cache.clear()
    yield
    images._encode_cache.clear()


# ── sniff / ref / placeholder ───────────────────────────────────────


class TestRef:
    def test_sniff_four_formats_only(self):
        assert images.sniff_image(b"\x89PNG\r\n\x1a\n" + b"\0" * 8) == "image/png"
        assert images.sniff_image(b"\xff\xd8\xff\xe0" + b"\0" * 12) == "image/jpeg"
        assert images.sniff_image(b"GIF89a" + b"\0" * 10) == "image/gif"
        assert images.sniff_image(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
        assert images.sniff_image(b"BM" + b"\0" * 14) is None  # BMP: 공급자가 안 받음
        assert images.sniff_image(b"plain text") is None

    def test_make_ref_reads_size_and_hash(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = make_png(tmp_path / "shot.png", size=(120, 80))
        ref = images.make_ref(p)
        assert (ref.path, ref.mime, ref.width, ref.height) == (
            "shot.png",
            "image/png",
            120,
            80,
        )
        assert ref.bytes == p.stat().st_size
        assert len(ref.sha256) == 64
        assert ref.placeholder == "[image: shot.png (120×80)]"
        assert images.is_placeholder_line(ref.placeholder)
        assert ImageRef.from_dict(ref.to_dict()) == ref

    def test_make_ref_rejects_non_image(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hello")
        with pytest.raises(ValueError, match="not a PNG/JPEG/GIF/WebP"):
            images.make_ref(p)

    def test_tokens_are_pixels_per_constant(self):
        ref = ImageRef("a.png", "image/png", 1920, 1080, 1, "")
        assert ref.tokens == (1920 * 1080) // images.IMAGE_PX_PER_TOKEN
        assert ImageRef("a.png", "image/png", 1, 1, 1, "").tokens == 1  # 최소 1


class TestStageUpload:
    def test_inside_workspace_is_referenced_in_place(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = make_png(tmp_path / "docs" / "a.png")
        ref = images.stage_upload(p)
        assert ref.path == "docs/a.png"
        assert not (tmp_path / images.UPLOAD_DIR).exists()

    def test_outside_workspace_is_copied_to_uploads(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.chdir(ws)
        src = make_png(tmp_path / "elsewhere" / "cap.png")
        ref = images.stage_upload(src)
        assert ref.path.startswith(images.UPLOAD_DIR + "/")
        assert ref.path.endswith("-cap.png")
        assert (ws / ref.path).read_bytes() == src.read_bytes()

    def test_missing_or_non_image_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match="no such file"):
            images.stage_upload(tmp_path / "nope.png")
        t = tmp_path / "t.txt"
        t.write_text("x")
        with pytest.raises(ValueError, match="not a PNG"):
            images.stage_upload(t)


# ── keep-N / strip ──────────────────────────────────────────────────


def _msg(n_images: int, text="t") -> dict:
    return {
        "role": "user",
        "content": text,
        "images": [
            ImageRef(f"{text}{i}.png", "image/png", 10, 10, 1, "").to_dict()
            for i in range(n_images)
        ],
    }


class TestSummariserBoundary:
    def test_no_live_image_cap_any_more(self):
        """v10.77.0 — 살아 있는 수 상한은 지웠다: 대화 중간의 참조를 떼면 KV
        캐시가 그 뒤 전체로 깨지고 모델이 빠진 그림을 다시 읽었다."""
        assert not hasattr(images, "keep_recent")
        assert not hasattr(images, "image_keep")
        assert not hasattr(images, "IMAGE_KEEP_DEFAULT")
        assert not hasattr(images, "strip_images")  # 요약기도 그림을 본다

    def test_env_knobs(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_IMAGE_MAX_EDGE", "oops")
        assert images.image_max_edge() == images.IMAGE_MAX_EDGE_DEFAULT

    def test_mentions_image_finds_a_placeholder_mid_line(self):
        assert images.mentions_image(
            [{"role": "user", "content": "[view_image] → [image: a.png (2×2)]"}]
        )
        assert not images.mentions_image([{"role": "user", "content": "plain"}])
        assert not images.mentions_image([{"role": "user", "content": ["x"]}])


# ── 공급자 경계 — expand / encode ────────────────────────────────────


class TestExpand:
    def test_enabled_builds_parts_and_never_leaks_images_key(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        ref = images.make_ref(make_png(tmp_path / "a.png"))
        msgs = [
            {
                "role": "user",
                "content": "look\n" + ref.placeholder,
                "images": [ref.to_dict()],
            },
            {"role": "assistant", "content": "ok", "images": []},
        ]
        out = images.expand(msgs, enabled=True, part=images.openai_part)
        assert all("images" not in m for m in out)
        parts = out[0]["content"]
        assert parts[0] == {"type": "text", "text": "look\n" + ref.placeholder}
        assert parts[1]["type"] == "image_url"
        data = parts[1]["image_url"]["url"]
        assert data.startswith("data:image/png;base64,")
        assert (
            base64.b64decode(data.split(",", 1)[1]) == (tmp_path / "a.png").read_bytes()
        )
        assert out[1]["content"] == "ok"  # 이미지 없는 메시지는 문자열 그대로
        assert msgs[0]["images"]  # 입력은 안 건드린다

    def test_anthropic_part_shape(self):
        assert images.anthropic_part("image/png", "QUJD") == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
        }

    def test_disabled_keeps_text_and_says_so(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ref = images.make_ref(make_png(tmp_path / "a.png"))
        out = images.expand(
            [{"role": "user", "content": ref.placeholder, "images": [ref.to_dict()]}],
            enabled=False,
            part=images.openai_part,
        )
        assert isinstance(out[0]["content"], str)
        assert out[0]["content"].startswith(ref.placeholder)
        assert "cannot view images" in out[0]["content"]
        assert "images" not in out[0]

    def test_missing_file_becomes_a_note(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ref = ImageRef("gone.png", "image/png", 10, 10, 1, "")
        out = images.expand(
            [{"role": "user", "content": "q", "images": [ref.to_dict()]}],
            enabled=True,
            part=images.openai_part,
        )
        assert out[0]["content"] == "q\n(image gone.png not shown: file missing)"

    def test_changed_file_sends_the_recorded_image(self, tmp_path, monkeypatch):
        """덮어써도 기록된 그림이 간다 (v10.101.0 보관본) — 대화 앞부분이
        바뀌지 않는다(화면 기록과 같고 KV 캐시도 산다)."""
        monkeypatch.chdir(tmp_path)
        p = make_png(tmp_path / "a.png")
        recorded = base64.b64encode(p.read_bytes()).decode("ascii")
        ref = images.make_ref(p)
        make_png(p, color=(0, 255, 0))  # 기록 뒤 바뀐 파일
        out = images.expand(
            [{"role": "user", "content": "q", "images": [ref.to_dict()]}],
            enabled=True,
            part=images.openai_part,
        )
        assert out[0]["content"][1]["image_url"]["url"].endswith(recorded)

    def test_changed_file_without_a_kept_copy_is_not_sent(self, tmp_path, monkeypatch):
        """보관본이 없으면(v10.101.0 이전 기록) 종전대로 해시로 원본을 확인한다."""
        monkeypatch.chdir(tmp_path)
        p = make_png(tmp_path / "a.png")
        ref = images.make_ref(p)
        images.stored_path(ref.sha256, ref.mime).unlink()
        make_png(p, color=(0, 255, 0))
        out = images.expand(
            [{"role": "user", "content": "q", "images": [ref.to_dict()]}],
            enabled=True,
            part=images.openai_part,
        )
        assert "file changed since it was recorded" in out[0]["content"]
        assert isinstance(out[0]["content"], str)

    def test_encode_is_cached_per_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ref = images.make_ref(make_png(tmp_path / "a.png"))
        first = images.encode(ref)
        assert images.encode(ref) == first
        assert len(images._encode_cache) == 1


class TestDownscale:
    def test_long_edge_over_cap_is_shrunk(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AGENT_CLI_IMAGE_MAX_EDGE", "100")
        ref = images.make_ref(make_png(tmp_path / "big.png", size=(400, 200)))
        mime, b64 = images.encode(ref)
        with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
            assert im.size == (100, 50)
        assert mime == "image/png"

    def test_small_image_is_sent_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = make_png(tmp_path / "s.png", size=(40, 20))
        ref = images.make_ref(p)
        _mime, b64 = images.encode(ref)
        assert base64.b64decode(b64) == p.read_bytes()

    def test_jpeg_stays_jpeg_when_shrunk(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AGENT_CLI_IMAGE_MAX_EDGE", "50")
        p = tmp_path / "p.jpg"
        Image.new("RGB", (200, 100), (1, 2, 3)).save(p, format="JPEG")
        mime, b64 = images.encode(images.make_ref(p))
        assert mime == "image/jpeg"
        assert base64.b64decode(b64)[:3] == b"\xff\xd8\xff"

    def test_zero_edge_disables_downscale(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AGENT_CLI_IMAGE_MAX_EDGE", "0")
        p = make_png(tmp_path / "b.png", size=(3000, 10))
        _mime, b64 = images.encode(images.make_ref(p))
        assert base64.b64decode(b64) == p.read_bytes()


# ── 능력 프로브 ──────────────────────────────────────────────────────


class TestProbe:
    def test_probe_image_is_green_left_blue_right(self):
        mime, b64 = images.probe_image()
        assert mime == "image/png"
        with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
            assert im.size == (128, 64)
            assert im.getpixel((10, 10))[1] > 100 and im.getpixel((10, 10))[2] == 0
            assert im.getpixel((100, 10))[2] == 255 and im.getpixel((100, 10))[1] == 0

    def test_answer_must_name_both_colors_in_order(self):
        assert images.probe_answer_ok("Green and blue.")
        assert images.probe_answer_ok("left: GREEN, right: BLUE")
        assert not images.probe_answer_ok("blue and green")  # 순서 반대
        assert not images.probe_answer_ok("I cannot see images")
        assert not images.probe_answer_ok("green")


# ── 접점: 툴 브리지 캡 ───────────────────────────────────────────────


def _bridge(window):
    from agent_cli.loop.state import LoopState
    from agent_cli.loop.tool_bridge import ToolBridge
    from agent_cli.tools import TOOLS
    from tests.binding_support import make_binding
    from tests.loopkit import make_config

    class _Caps:
        context_window = window
        max_output_tokens = 1000

    return ToolBridge(
        make_config(
            tools_list=list(TOOLS.keys()), binding=make_binding(capabilities=_Caps())
        ),
        LoopState(),
        ctx=None,
        provider=None,
    )


class TestBridgeObserve:
    def test_images_ride_with_small_result(self):
        from agent_cli.tools.result import ToolResult

        ref = ImageRef("a.png", "image/png", 100, 100, 1, "").to_dict()
        body, imgs = _bridge(250_000).observe(
            "read_file",
            ToolResult(True, output="[image: a.png (100×100)]", images=[ref]),
            {},
        )
        assert body == "[image: a.png (100×100)]"
        assert imgs == [ref]

    def test_image_alone_over_cap_is_dropped_with_note(self):
        from agent_cli.tools.result import ToolResult

        bridge = _bridge(250_000)  # cap 25,000 tokens
        huge = ImageRef(
            "h.png", "image/png", 10_000, 10_000, 1, ""
        ).to_dict()  # 100K tokens
        body, imgs = bridge.observe(
            "read_file", ToolResult(True, output="x", images=[huge]), {}
        )
        assert imgs == []
        assert body.startswith(
            "x\n[image not attached: ~100,000 tokens exceeds the 25,000-token"
        )

    def test_image_tokens_charge_the_turn_budget(self):
        from agent_cli.tools.result import ToolResult

        bridge = _bridge(250_000)
        ref = ImageRef(
            "a.png", "image/png", 2000, 2000, 1, ""
        ).to_dict()  # 4,000 tokens
        acc: list = []
        bridge.accumulate_observation(
            acc, "read_file", ToolResult(True, output="small", images=[ref]), {}
        )
        assert acc[0]["images"] == [ref]
        assert acc[0]["tokens"] >= 4000

    def test_tool_observation_text_only_wrapper(self):
        from agent_cli.tools.result import ToolResult

        ref = ImageRef("a.png", "image/png", 10, 10, 1, "").to_dict()
        assert (
            _bridge(250_000)._tool_observation(
                "read_file", ToolResult(True, output="t", images=[ref]), {}
            )
            == "t"
        )


# ── 접점: 루프의 사용자 메시지·관찰 레코드 ────────────────────────────


def _bare_loop():
    """``_add_user_message`` 만 부를 수 있는 최소 AgentLoop (생성자 없이)."""
    from types import SimpleNamespace

    from agent_cli.loop.core import AgentLoop
    from agent_cli.loop.state import LoopState
    from tests.loop_kinds import make_kind

    loop = AgentLoop.__new__(AgentLoop)
    loop._state = LoopState()
    loop._config = SimpleNamespace(kind=make_kind())
    loop.ctx = None
    loop.run_authors = []
    return loop


class TestLoopRecords:
    def test_add_user_message_appends_placeholder_and_images(self):

        loop = _bare_loop()
        ref = ImageRef("cap.png", "image/png", 640, 480, 1, "").to_dict()
        loop._add_user_message("what is this?", images=[ref])
        rec = loop.messages[-1]
        assert rec["content"] == "what is this?\n[image: cap.png (640×480)]"
        assert rec["images"] == [ref]

    def test_add_user_message_without_images_is_unchanged(self):

        loop = _bare_loop()
        loop._add_user_message("plain")
        assert loop.messages[-1] == {"role": "user", "content": "plain"}

    def test_append_observation_stores_images_on_the_record(self):
        from agent_cli.dialects import get as get_dialect
        from agent_cli.loop import _append_observation

        captured: list[dict] = []

        class _Ctx:
            last_ordinal = None

            def add(self, entry):
                captured.append(entry)
                return entry

        wf = get_dialect("json_fc")
        messages: list = []
        ref = ImageRef("a.png", "image/png", 10, 10, 1, "").to_dict()
        _append_observation(
            messages,
            _Ctx(),
            wf,
            wf.serialize_assistant_for_history("llm"),
            "Observation: [image: a.png (10×10)]",
            tool_name="read_file",
            success=True,
            render=False,
            images=[ref],
        )
        assert captured[-1]["images"] == [ref]
        assert messages[-1]["images"] == [ref]


class TestQueueAndRequest:
    def test_enqueue_carries_images_into_the_run_request(self):
        from agent_cli.input_queue import InputQueue
        from agent_cli.pump import RunRequest

        q = InputQueue()
        ref = ImageRef("a.png", "image/png", 10, 10, 1, "").to_dict()
        item = q.enqueue(None, "look", images=[ref])
        assert item["images"] == [ref]
        assert q.enqueue(None, "plain")["images"] == []
        import threading

        req = RunRequest(
            text="look",
            author=None,
            author_is_user=True,
            request_id="u-1",
            stop_event=threading.Event(),
            wake=False,
            images=list(item["images"]),
        )
        assert req.images == [ref]


class TestCliStaging:
    def test_stage_query_images_returns_dicts(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from agent_cli.main import _stage_query_images

        make_png(tmp_path / "a.png")
        refs = _stage_query_images(["a.png"])
        assert refs[0]["path"] == "a.png" and refs[0]["width"] == 64
        assert _stage_query_images(None) == []

    def test_stage_query_images_bad_path_exits(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import typer

        from agent_cli.main import _stage_query_images

        with pytest.raises(typer.Exit):
            _stage_query_images([str(tmp_path / "missing.png")])

    def test_require_vision_exits_when_model_cannot_see(self):
        import typer

        from agent_cli.main import _require_vision
        from agent_cli.providers.capabilities import ModelCapabilities

        class _Provider:
            @staticmethod
            def capability_transport(base_url, model, api_key=""):
                raise AssertionError("registered value must short-circuit the probe")

        class _Binding:
            capabilities = ModelCapabilities(10_000, 100, False, supports_vision=False)
            provider = _Provider()
            base_url = "http://x"
            model = "m"
            api_key = ""

        with pytest.raises(typer.Exit):
            _require_vision(_Binding())
        _Binding.capabilities = ModelCapabilities(
            10_000, 100, False, supports_vision=True
        )
        _require_vision(_Binding())  # 통과


# ── P2 웹 (v10.60.0) — 공용 업로드 자리·주입·콘솔 줄 ────────────────────


class TestFreshUploadPath:
    def test_makes_the_dir_and_never_collides(self, tmp_path):
        from agent_cli.images import UPLOAD_DIR, fresh_upload_path

        a = fresh_upload_path(tmp_path, "shot.png")
        assert a.parent == tmp_path / UPLOAD_DIR and a.parent.is_dir()
        assert a.name.endswith("-shot.png")
        a.write_bytes(b"x")
        b = fresh_upload_path(tmp_path, "shot.png")
        assert b != a and not b.exists()


class TestInjectedMessageImages:
    """턴 경계 주입(web 큐)도 런 시작 질의처럼 이미지를 싣는다."""

    def test_injected_item_images_reach_record_and_echo(self):
        from types import SimpleNamespace

        from agent_cli import render

        loop = _bare_loop()
        loop.messages = []
        loop.task_log = []
        loop.route_message = None
        from tests.loop_kinds import make_kind
        from tests.loop_ports import make_ports

        ref = ImageRef("cap.png", "image/png", 640, 480, 1, "").to_dict()
        items = [{"id": "u-1", "nickname": "dj", "text": "see", "images": [ref]}]
        # v10.88.0: 어디서 받을지는 종류 — 입력 큐 포트가 있는 맨 종류
        loop._config = SimpleNamespace(
            kind=make_kind(
                ports=make_ports(
                    dequeue_user_message=lambda: items.pop(0) if items else None
                )
            ),
        )

        class _Stub:
            def __init__(self):
                self.pushed: list = []

            def push_user_message(self, content, author="", hidx=None, images=None):
                self.pushed.append((content, author, images))

            def set_run_authors(self, authors):
                pass

        stub = _Stub()
        prev = render.get_renderer()
        render.set_renderer(stub)
        try:
            loop._intake()
        finally:
            render.set_renderer(prev)
        assert stub.pushed == [("[dj]: see", "dj", [ref])]
        rec = loop.messages[-1]
        assert rec["content"] == "[dj]: see\n[image: cap.png (640×480)]"
        assert rec["images"] == [ref]


class TestMinimalEchoImages:
    def test_injected_echo_prints_an_image_line(self, capsys):
        from io import StringIO

        from rich.console import Console

        from agent_cli.render.minimal import MinimalRenderer

        buf = StringIO()
        r = MinimalRenderer(Console(file=buf, width=200))
        ref = ImageRef("cap.png", "image/png", 640, 480, 1, "").to_dict()
        r.push_user_message("[dj]: see", author="dj", images=[ref])
        out = buf.getvalue()
        assert "📨 [dj]: see" in out
        assert "🖼 cap.png (640×480)" in out


class TestWebNoVisionNotice:
    def test_vision_ok_uses_registered_value(self):
        from agent_cli.providers.capabilities import (
            ModelCapabilities,
            binding_sees_images,
        )
        from agent_cli.web.surface import NO_VISION_NOTICE

        class _Provider:
            @staticmethod
            def capability_transport(base_url, model, api_key=""):
                raise AssertionError("registered value must short-circuit the probe")

        class _Binding:
            capabilities = ModelCapabilities(10_000, 100, False, supports_vision=False)
            provider = _Provider()
            base_url = "http://x"
            model = "m"
            api_key = ""

        assert binding_sees_images(_Binding()) is False
        assert "cannot view images" in NO_VISION_NOTICE


# ── Pillow 선택 의존성 (v10.61.1) — 크기·프로브는 표준 라이브러리, 축소만 Pillow ──


class TestImageSizeWithoutPillow:
    @staticmethod
    def _bytes(fmt, size=(37, 19), **kw):
        import io

        buf = io.BytesIO()
        Image.new("RGB", size, (10, 20, 30)).save(buf, format=fmt, **kw)
        return buf.getvalue()

    def test_four_formats(self):
        from agent_cli.images import image_size

        assert image_size(self._bytes("PNG")) == (37, 19)
        assert image_size(self._bytes("JPEG")) == (37, 19)
        assert image_size(self._bytes("JPEG", progressive=True)) == (37, 19)
        assert image_size(self._bytes("GIF")) == (37, 19)
        try:
            webp = self._bytes("WEBP")
        except Exception:
            pytest.skip("Pillow without libwebp")
        assert image_size(webp) == (37, 19)
        assert image_size(self._bytes("WEBP", lossless=True)) == (37, 19)
        assert image_size(b"not an image") is None
        assert image_size(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4) is None  # 잘린 헤더

    def test_make_ref_and_probe_need_no_pillow(self, tmp_path, monkeypatch):
        import base64
        import sys

        from agent_cli import images as I

        p = make_png(tmp_path / "a.png", size=(50, 40))
        # PIL 을 import 하면 터지게 — make_ref·probe_image 는 건드리지 않아야 한다
        monkeypatch.setitem(sys.modules, "PIL", None)
        monkeypatch.setitem(sys.modules, "PIL.Image", None)
        ref = I.make_ref(p)
        assert (ref.width, ref.height, ref.mime) == (50, 40, "image/png")
        assert ref.sha256 and ref.bytes == p.stat().st_size
        mime, b64 = I.probe_image()
        assert mime == "image/png" and I.image_size(base64.b64decode(b64)) == (128, 64)

    def test_probe_constant_is_left_green_right_blue(self):
        import base64
        import io

        from agent_cli.images import probe_image

        _, b64 = probe_image()
        with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
            im = im.convert("RGB")
            assert im.size == (128, 64)
            assert im.getpixel((10, 32)) == (0, 160, 0)  # 왼쪽 green
            assert im.getpixel((118, 32)) == (0, 0, 255)  # 오른쪽 blue


class TestEncodeWithoutPillow:
    def test_original_is_sent_as_is(self, tmp_path, monkeypatch):
        from agent_cli import images as I

        monkeypatch.setattr(I, "has_pillow", lambda: False)
        monkeypatch.setenv("AGENT_CLI_IMAGE_MAX_EDGE", "64")
        p = make_png(tmp_path / "big.png", size=(300, 200))  # 상한보다 크다
        ref = I.make_ref(p)
        I._encode_cache.clear()
        mime, b64 = I.encode(ref)
        assert mime == "image/png"
        import base64

        assert base64.b64decode(b64) == p.read_bytes()  # 축소 없음, 원본 그대로

    def test_over_byte_cap_is_a_reason_not_a_crash(self, tmp_path, monkeypatch):
        from agent_cli import images as I

        monkeypatch.setattr(I, "has_pillow", lambda: False)
        monkeypatch.setattr(I, "IMAGE_MAX_BYTES", 100)
        p = make_png(tmp_path / "big.png", size=(300, 200))
        ref = I.make_ref(p)
        I._encode_cache.clear()
        got = I.encode(ref)
        assert isinstance(got, str)
        assert got.startswith("too large (") and "pip install pillow" in got

    def test_with_pillow_still_downscales(self, tmp_path, monkeypatch):
        from agent_cli import images as I

        monkeypatch.setenv("AGENT_CLI_IMAGE_MAX_EDGE", "64")
        p = make_png(tmp_path / "big.png", size=(300, 200))
        I._encode_cache.clear()
        _mime, b64 = I.encode(I.make_ref(p))
        import base64

        assert I.image_size(base64.b64decode(b64)) == (64, 43)


class TestStore:
    """보관소 (v10.101.0) — 참조를 만들 때 내용을 ``<sha256>.<ext>`` 로 남긴다."""

    def test_make_ref_keeps_a_copy_once(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = make_png(tmp_path / "a.png")
        ref = images.make_ref(p)
        kept = images.stored_path(ref.sha256, "image/png")
        assert kept.name == f"{ref.sha256}.png"
        assert kept.read_bytes() == p.read_bytes()
        mtime = kept.stat().st_mtime_ns
        images.make_ref(p)  # 같은 내용 — 다시 쓰지 않는다
        assert kept.stat().st_mtime_ns == mtime
        assert [x.name for x in kept.parent.iterdir()] == [kept.name]  # 임시 파일 없음

    def test_resolve_prefers_the_copy_then_a_matching_original(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        p = make_png(tmp_path / "a.png")
        ref = images.make_ref(p)
        assert images.resolve_file(ref) == images.stored_path(ref.sha256, ref.mime)
        images.stored_path(ref.sha256, ref.mime).unlink()
        assert Path(images.resolve_file(ref)).resolve() == p.resolve()
        p.unlink()
        assert images.resolve_file(ref) == "file missing"

    def test_resolve_by_hash_alone(self, tmp_path, monkeypatch):
        """웹 ``?v=`` 는 해시만 안다 — mime 없이도 보관본을 찾는다."""
        monkeypatch.chdir(tmp_path)
        ref = images.make_ref(make_png(tmp_path / "a.png"))
        bare = ImageRef(
            path="elsewhere.png", mime="", width=0, height=0, bytes=0, sha256=ref.sha256
        )
        assert images.resolve_file(bare) == images.stored_path(ref.sha256, ref.mime)

    def test_unwritable_store_still_makes_the_ref(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        blocker = tmp_path / "store"
        blocker.write_text("not a directory")
        monkeypatch.setattr(images, "_STORE_DIR", blocker)
        p = make_png(tmp_path / "a.png")
        ref = images.make_ref(p)
        assert ref.width == 64
        assert Path(images.resolve_file(ref)).resolve() == p.resolve()  # 원본을 해시로

    def test_default_store_is_cwd_relative(self, tmp_path, monkeypatch):
        """운영 구성 — conftest 가 돌려 둔 자리 대신 ``paths.images_dir()`` 그대로."""
        from agent_cli import paths

        monkeypatch.setattr(images, "_STORE_DIR", paths.images_dir())
        monkeypatch.chdir(tmp_path)
        ref = images.make_ref(make_png(tmp_path / "a.png"))
        assert (tmp_path / ".agent-cli" / "images" / f"{ref.sha256}.png").is_file()

    def test_interleaved_stores_use_separate_temp_files(self, tmp_path, monkeypatch):
        """같은 그림을 두 스레드(서브에이전트)가 동시에 보관할 때 — 한쪽이 교체하기
        직전에 다른 쪽이 끼어들어도 서로의 임시 파일을 자르지 않는다. 끼어들기를
        ``os.replace`` 안에서 결정적으로 재현한다(종전 pid 이름은 둘이 같았다)."""
        monkeypatch.chdir(tmp_path)
        data = make_png(tmp_path / "a.png", size=(400, 300)).read_bytes()
        sha = "f" * 64
        real_replace = os.replace
        sources: list[str] = []

        def replace(src, dst):
            sources.append(str(src))
            if len(sources) == 1:
                images._store(sha, "image/png", data)  # 다른 스레드가 끼어든다
            real_replace(src, dst)

        monkeypatch.setattr(images.os, "replace", replace)
        images._store(sha, "image/png", data)
        assert len(sources) == 2 and sources[0] != sources[1]
        dest = images.stored_path(sha, "image/png")
        assert dest.read_bytes() == data
        assert [p.name for p in dest.parent.iterdir()] == [dest.name]  # 임시 파일 없음


# ── 디코딩 불가 이미지 (v10.113.0, docs/vision/DESIGN.md §13) ─────────────


def bad_png(path: Path, *, idat: bytes | None = b"\x78\x9c\x63") -> Path:
    """시그니처·IHDR 는 정상이고 본문(IDAT)이 잘렸거나(기본) 없는 PNG — 모델이 손수
    만든 실물과 같은 모양을 테스트가 직접 만든다(실물 복사 아님)."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    body = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", 40, 20, 8, 2, 0, 0, 0)
    )
    if idat is not None:
        body += chunk(b"IDAT", idat)
    body += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


class TestDecodeCheck:
    def test_truncated_and_missing_idat_are_caught(self, tmp_path):
        for name, idat in (("cut.png", b"\x78\x9c\x63"), ("none.png", None)):
            data = bad_png(tmp_path / name, idat=idat).read_bytes()
            # 헤더 검사는 통과한다 — 그래서 본문까지 풀어 봐야 한다
            assert images.sniff_image(data[:16]) == "image/png"
            assert images.image_size(data) == (40, 20)
            assert images.decode_check(data), name

    def test_verify_would_pass_but_load_does_not(self, tmp_path):
        """`verify()` 가 아니라 `load()` 인 이유 — 본문이 잘린 PNG 를 verify 는 통과시킨다."""
        data = bad_png(tmp_path / "cut.png").read_bytes()
        with Image.open(io.BytesIO(data)) as im:
            im.verify()  # 예외 없음
        assert images.decode_check(data)

    def test_a_good_png_passes(self, tmp_path):
        assert images.decode_check(make_png(tmp_path / "ok.png").read_bytes()) is None

    def test_without_pillow_it_cannot_check(self, tmp_path, monkeypatch):
        monkeypatch.setattr(images, "has_pillow", lambda: False)
        assert images.decode_check(bad_png(tmp_path / "cut.png").read_bytes()) is None


class TestEncodeUndecodable:
    def test_small_damaged_png_is_a_reason_not_a_part(self, tmp_path):
        """작아서 축소하지 않는 손상본도 본문을 푼다 — 종전엔 원본 그대로 조각이 되어
        서버가 요청을 400 으로 거절했다."""
        p = bad_png(tmp_path / "cut.png")
        with pytest.raises(Exception, match="truncated|broken|decompress|not enough"):
            images._encode_bytes(p.read_bytes(), "image/png")
        got = images.encode(images.make_ref(p))
        assert isinstance(got, str) and got.startswith("cannot encode (")
        assert got.endswith(f"— {images.UNDECODABLE_HINT}")

    def test_the_failure_reason_is_cached(self, tmp_path, monkeypatch):
        """실패 사유도 같은 키로 캐시한다 — 레코드에 남은 손상본을 턴마다 다시 읽고
        다시 풀지 않는다(큰 잘린 파일이면 턴마다 비용)."""
        ref = images.make_ref(bad_png(tmp_path / "cut.png"))
        decodes, reads = [], []
        real_encode, real_read = images._encode_bytes, Path.read_bytes

        def counting_encode(data, mime):
            decodes.append(1)
            return real_encode(data, mime)

        def counting_read(self):
            reads.append(self.name)
            return real_read(self)

        monkeypatch.setattr(images, "_encode_bytes", counting_encode)
        monkeypatch.setattr(Path, "read_bytes", counting_read)
        first = images.encode(ref)
        second = images.encode(ref)
        assert isinstance(first, str) and second == first
        assert len(decodes) == 1 and len(reads) == 1  # 두 번째는 읽지도 풀지도 않는다

    def test_too_large_reason_is_cached_too(self, tmp_path, monkeypatch):
        monkeypatch.setattr(images, "has_pillow", lambda: False)
        monkeypatch.setattr(images, "IMAGE_MAX_BYTES", 100)
        ref = images.make_ref(make_png(tmp_path / "big.png", size=(300, 200)))
        calls = []
        real = images._encode_bytes
        monkeypatch.setattr(
            images, "_encode_bytes", lambda d, m: calls.append(1) or real(d, m)
        )
        first = images.encode(ref)
        assert first.startswith("too large (") and images.encode(ref) == first
        assert len(calls) == 1

    def test_expand_puts_the_reason_line_instead_of_a_part(self, tmp_path):
        p = bad_png(tmp_path / "cut.png")
        ref = images.make_ref(p)
        msgs = [{"role": "user", "content": "look", "images": [ref.to_dict()]}]
        (m,) = images.expand(msgs, enabled=True, part=images.openai_part)
        assert isinstance(m["content"], str)  # 조각 없음
        assert f"(image {ref.path} not shown: cannot encode (" in m["content"]
        assert images.UNDECODABLE_HINT in m["content"]


class TestUndecodableServerError:
    @staticmethod
    def _http(status: int, body: str):
        import requests

        r = requests.Response()
        r.status_code = status
        return requests.HTTPError(f"{status} Error: {body}", response=r)

    OMLX = (
        '{"error":{"message":"image_url does not contain a decodable image.",'
        '"type":"invalid_request_error","param":"image_url"}}'
    )

    def test_400_with_the_phrase(self):
        assert images.is_undecodable_image_error(self._http(400, self.OMLX))

    def test_400_with_another_phrase(self):
        assert not images.is_undecodable_image_error(
            self._http(400, '{"error":{"message":"bad request"}}')
        )

    def test_status_gate(self):
        """문구만으로는 안 된다 — 500 이면 같은 문구라도 거짓."""
        assert not images.is_undecodable_image_error(self._http(500, self.OMLX))

    def test_not_an_http_error(self):
        assert not images.is_undecodable_image_error(ValueError(self.OMLX))


class TestExpandBlocked:
    def test_blocked_sha_becomes_a_reason_line(self, tmp_path):
        good = images.make_ref(make_png(tmp_path / "a.png"))
        other = images.make_ref(make_png(tmp_path / "b.png", color=(0, 255, 0)))
        msgs = [
            {
                "role": "user",
                "content": "two",
                "images": [good.to_dict(), other.to_dict()],
            }
        ]
        (m,) = images.expand(
            msgs, enabled=True, part=images.openai_part, blocked={good.sha256}
        )
        parts = [p for p in m["content"] if p.get("type") == "image_url"]
        assert len(parts) == 1  # 막힌 그림만 빠진다
        assert m["content"][0]["text"].endswith(
            f"(image {good.path} not shown: {images.SERVER_UNDECODABLE})"
        )
        assert images.SERVER_UNDECODABLE == (
            "the server could not decode it — the file is damaged or incomplete; "
            "regenerate it"
        )
