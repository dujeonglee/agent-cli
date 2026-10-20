"""이미지 입력 (vision, v10.59.0 — docs/vision/DESIGN.md).

레코드·``messages`` 의 ``content`` 는 끝까지 문자열이다. 이미지는 레코드의
별도 필드 ``images: [ImageRef]`` (경로·크기·해시, base64 없음) 로 실리고,
**공급자 경계** (:func:`expand`) 에서만 조각(parts)으로 펼쳐진다. 그래서
꼬리 붙이기·토큰 추정·압축·재생·세션 목록은 문자열 가정을 그대로 둔다.

파일이 진실, 기록은 참조: 보낼 때 파일에서 읽고(해시로 같은 파일인지 확인),
없으면 글 자리표시로 대신한다. 자리표시 ``[image: path (W×H)]`` 는 레코드의
``content`` 에도 들어 있어 이미지가 못 가는 경로(요약기·텍스트 전용 모델·
파일 소실)에서도 같은 레코드가 그대로 읽힌다.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: 토큰 추정 — 픽셀 수 / 이 값. Qwen3.6(omlx) 실측 ≈ 1K px/token
#: (480×240 ≈ 130, 1280×720 ≈ 890, 1920×1080 ≈ 2,050). Anthropic 공식은
#: ≈ 750 — 차이는 서버 실측 재조정(``reconcile_actual_tokens``)이 흡수한다.
IMAGE_PX_PER_TOKEN = 1000
#: 살아 있는 이미지 수 상한 기본값 — 최근 N 장만 픽셀을 보내고, 더 오래된
#: 레코드는 자리표시 글만 남긴다 (사용자 결정, 실측 뒤 조정). 0 = 무제한.
IMAGE_KEEP_DEFAULT = 6
#: 보내기 전 긴 변 상한 기본값 — 넘으면 Pillow 로 축소 (Anthropic 권장 ≤1568,
#: OpenAI 고해상도 2048). 실측(§1: 1280×720 에서 60px 글자 오독) 뒤 조정.
IMAGE_MAX_EDGE_DEFAULT = 2048
#: 인코딩 뒤 바이트 상한 (Anthropic 한도 5MB) — 넘으면 JPEG 로 다시 인코딩.
IMAGE_MAX_BYTES = 5_000_000
#: 워크스페이스 밖 파일을 들여오는 자리 (세션 재개 때도 남아야 하므로
#: 세션 디렉토리가 아니라 tmp — 사용자 결정).
UPLOAD_DIR = ".agent-cli/tmp/uploads"
#: browser `screenshot` 모드가 PNG 를 두는 자리 (v10.61.0).
SCREENSHOT_DIR = ".agent-cli/tmp/screenshots"
#: MCP 도구가 돌려준 이미지 조각을 파일로 두는 자리 (v10.61.0).
MCP_IMAGE_DIR = ".agent-cli/tmp/mcp"
#: mime → 파일 확장자 (저장 이름용).
IMAGE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
#: 두 공급자가 받는 형식 — 매직 바이트로 판정한다 (확장자는 믿지 않는다).
IMAGE_MIMES = ("image/png", "image/jpeg", "image/gif", "image/webp")

_PLACEHOLDER_PREFIX = "[image: "
_NOT_SHOWN_TEXT_MODEL = (
    "(image not shown: this model cannot view images — describe it another way, "
    "e.g. run a script that reads the file)"
)


def image_keep() -> int:
    """``AGENT_CLI_IMAGE_KEEP`` — 살아 있는 이미지 수 상한 (0 = 무제한)."""
    return _env_int("AGENT_CLI_IMAGE_KEEP", IMAGE_KEEP_DEFAULT)


def image_max_edge() -> int:
    """``AGENT_CLI_IMAGE_MAX_EDGE`` — 보내기 전 긴 변 상한 (0 = 축소 없음)."""
    return _env_int("AGENT_CLI_IMAGE_MAX_EDGE", IMAGE_MAX_EDGE_DEFAULT)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def sniff_image(head: bytes) -> str | None:
    """앞 바이트로 이미지 형식 판정 — 공급자가 받는 넷만. 아니면 None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_file(path: Path) -> str | None:
    """파일의 이미지 형식 — 못 읽거나 이미지가 아니면 None."""
    try:
        with open(path, "rb") as f:
            return sniff_image(f.read(16))
    except OSError:
        return None


@dataclass(frozen=True)
class ImageRef:
    """레코드의 이미지 참조 — history.jsonl 에 그대로 적힌다 (base64 없음)."""

    path: str  # 모델이 보는 경로 (``display_path`` 규칙: 워크스페이스 안이면 상대)
    mime: str
    width: int
    height: int
    bytes: int
    sha256: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "mime": self.mime,
            "width": self.width,
            "height": self.height,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ImageRef:
        return cls(
            path=str(d.get("path") or ""),
            mime=str(d.get("mime") or ""),
            width=int(d.get("width") or 0),
            height=int(d.get("height") or 0),
            bytes=int(d.get("bytes") or 0),
            sha256=str(d.get("sha256") or ""),
        )

    @property
    def tokens(self) -> int:
        """픽셀 기준 추정 토큰 (``IMAGE_PX_PER_TOKEN``)."""
        return max(1, (self.width * self.height) // IMAGE_PX_PER_TOKEN)

    @property
    def placeholder(self) -> str:
        """``content`` 에 남는 자리표시 한 줄."""
        return f"{_PLACEHOLDER_PREFIX}{self.path} ({self.width}×{self.height})]"


def is_placeholder_line(line: str) -> bool:
    return line.startswith(_PLACEHOLDER_PREFIX)


def make_ref(path: str | Path) -> ImageRef:
    """파일 → :class:`ImageRef`. 이미지가 아니면 ``ValueError``.

    크기는 헤더를 직접 읽는다(:func:`image_size`, Pillow 불필요 — v10.61.1).
    ``path`` 는 ``display_path`` 규칙으로 적는다 — 워크스페이스 안이면 상대.
    """
    from agent_cli.tools.base import display_path

    p = Path(path)
    data = p.read_bytes()
    mime = sniff_image(data[:16])
    if mime is None:
        raise ValueError(f"{path}: not a PNG/JPEG/GIF/WebP image")
    size = image_size(data)
    if size is None:
        raise ValueError(f"{path}: image header is damaged (no size)")
    return ImageRef(
        path=display_path(p),
        mime=mime,
        width=size[0],
        height=size[1],
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def image_size(data: bytes) -> tuple[int, int] | None:
    """헤더만 읽어 ``(가로, 세로)`` — PNG·GIF·JPEG·WebP, 표준 라이브러리만 (v10.61.1).

    Pillow 를 필수에서 뺀 자리: 사내 미러에 Pillow 가 없는 배포가 있었다. 크기는
    토큰 추정·자리표시·썸네일 표시의 재료라 **의존성 0** 으로 둔다. 알 수 없으면
    None(호출자가 손상 파일로 처리).
    """
    import struct

    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return (w, h) if w and h else None
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return (w, h) if w and h else None
    if data[:2] == b"\xff\xd8":
        # SOFn 마커(C0..CF, DHT C4·JPG C8·DAC CC 제외)까지 세그먼트를 건너뛴다.
        i = 2
        n = len(data)
        while i + 4 <= n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xFF, 0x01) or 0xD0 <= marker <= 0xD8:
                i += 1 if marker == 0xFF else 2
                continue
            seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 9 <= n:
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return (w, h) if w and h else None
                return None
            if marker == 0xD9 or marker == 0xDA:  # EOI / SOS — SOF 가 없었다
                return None
            i += 2 + seg_len
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return (w & 0x3FFF, h & 0x3FFF)
        if chunk == b"VP8L" and len(data) >= 25:
            b = data[21:25]
            w = ((b[1] & 0x3F) << 8 | b[0]) + 1
            h = ((b[3] & 0x0F) << 10 | b[2] << 2 | (b[1] & 0xC0) >> 6) + 1
            return (w, h)
        if chunk == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return (w, h)
        return None
    return None


def has_pillow() -> bool:
    """축소·재인코딩에 쓰는 선택 의존성 Pillow 가 있는가 (``agent-cli[image]``)."""
    import importlib.util

    return importlib.util.find_spec("PIL") is not None


def stage_upload(src: str | Path, workspace: Path | None = None) -> ImageRef:
    """사용자가 건넨 파일을 레코드가 가리킬 수 있는 자리로 들여온다.

    워크스페이스 안 파일은 그대로 참조한다. 밖의 파일은 ``UPLOAD_DIR`` 로
    복사한다(``<epoch>-<이름>``) — 세션 재개 때도 같은 경로가 살아 있도록.
    """
    src_p = Path(src).expanduser()
    if not src_p.is_file():
        raise ValueError(f"{src}: no such file")
    if sniff_file(src_p) is None:
        raise ValueError(f"{src}: not a PNG/JPEG/GIF/WebP image")
    root = (workspace or Path.cwd()).resolve()
    try:
        src_p.resolve().relative_to(root)
        return make_ref(src_p)
    except ValueError:
        pass
    dest = fresh_upload_path(root, src_p.name)
    shutil.copyfile(src_p, dest)
    return make_ref(dest)


def fresh_upload_path(workspace: Path, name: str, *, subdir: str = UPLOAD_DIR) -> Path:
    """``<workspace>/<subdir>/<epoch>-<name>`` — 비어 있는 새 자리(디렉토리는
    만든다). CLI ``--image`` 복사와 웹 업로드(v10.60.0)는 ``UPLOAD_DIR``,
    browser 스크린샷은 ``SCREENSHOT_DIR``, MCP 이미지는 ``MCP_IMAGE_DIR``
    (v10.61.0) — 전부 같은 규칙으로 이름을 짓는다; ``name`` 은 호출자가 한
    조각(경로 없음)으로 준다."""
    dest_dir = workspace / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{int(time.time())}-{name}"
    n = 1
    while dest.exists():
        n += 1
        dest = dest_dir / f"{int(time.time())}-{n}-{name}"
    return dest


# ── 토큰 ──────────────────────────────────────────────────────────


def message_image_tokens(msg: dict) -> int:
    """메시지(레코드·요청 메시지 공통 모양)의 이미지 추정 토큰."""
    refs = msg.get("images")
    if not isinstance(refs, list) or not refs:
        return 0
    return sum(ImageRef.from_dict(r).tokens for r in refs if isinstance(r, dict))


def messages_image_tokens(messages: list[dict]) -> int:
    return sum(message_image_tokens(m) for m in messages)


# ── 살아 있는 수 상한 ──────────────────────────────────────────────


def keep_recent(messages: list[dict], keep: int) -> list[dict]:
    """최근 ``keep`` 장만 ``images`` 를 남기고, 더 오래된 메시지의 참조는 뗀다.

    기록은 그대로다 — 요청을 조립할 때만 적용한다. 자리표시 글은 ``content``
    에 이미 있으므로 모델은 "여기 이미지가 있었다" 를 계속 안다. ``keep=0``
    은 무제한. 메시지 dict 는 복사해 돌려준다(캐시된 렌더를 건드리지 않는다).
    """
    if keep <= 0:
        return messages
    out = list(messages)
    seen = 0
    for i in range(len(out) - 1, -1, -1):
        refs = out[i].get("images")
        if not isinstance(refs, list) or not refs:
            continue
        if seen >= keep:
            m = dict(out[i])
            del m["images"]
            out[i] = m
            continue
        room = keep - seen
        if len(refs) > room:
            m = dict(out[i])
            m["images"] = list(refs[-room:])
            out[i] = m
        seen += min(len(refs), room)
    return out


def strip_images(messages: list[dict]) -> list[dict]:
    """``images`` 를 전부 뗀 사본 — 요약기 호출처럼 이미지를 싣지 않는 길."""
    return [
        ({k: v for k, v in m.items() if k != "images"} if "images" in m else m)
        for m in messages
    ]


# ── 공급자 경계 — 파일에서 읽어 조각으로 ─────────────────────────────

#: 세션당 ``(path, size, mtime_ns) → (sha256, mime, b64)`` — 같은 이미지가
#: 턴마다 다시 인코딩·축소되지 않게.
_encode_cache: dict[tuple[str, int, int], tuple[str, str, str]] = {}


#: Pillow 없이 바이트 상한을 넘었을 때 공급자 경계가 적는 사유 (v10.61.1).
_TOO_LARGE_NO_PILLOW = (
    "too large ({size:,} bytes > {cap:,}) — downscale it first; "
    "`pip install pillow` (agent-cli[image]) enables automatic downscaling"
)


class ImageTooLarge(ValueError):
    """Pillow 가 없어 축소하지 못한 채 바이트 상한을 넘은 이미지."""


def _encode_bytes(data: bytes, mime: str) -> tuple[str, bytes]:
    """긴 변 상한·바이트 상한에 맞춘 ``(mime, bytes)``.

    Pillow 가 있으면 축소·재인코딩한다. 없으면(선택 의존성, v10.61.1) 원본을 그대로
    돌려준다 — 실측(§8.1)에서 축소는 정확도를 올리지 않고 토큰만 아끼므로 기능은
    온전하다. 다만 바이트 상한(5MB)은 공급자 한도라 넘으면 :class:`ImageTooLarge`.
    """
    max_edge = image_max_edge()
    if not has_pillow():
        if len(data) > IMAGE_MAX_BYTES:
            raise ImageTooLarge(
                _TOO_LARGE_NO_PILLOW.format(size=len(data), cap=IMAGE_MAX_BYTES)
            )
        return mime, data
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        if (max_edge and max(w, h) > max_edge) or len(data) > IMAGE_MAX_BYTES:
            im.load()
            if max_edge and max(w, h) > max_edge:
                im.thumbnail((max_edge, max_edge))
            out_mime = "image/jpeg" if mime == "image/jpeg" else "image/png"
            buf = io.BytesIO()
            if out_mime == "image/jpeg":
                im.convert("RGB").save(buf, format="JPEG", quality=90)
            else:
                im.save(buf, format="PNG")
            data, mime = buf.getvalue(), out_mime
            if len(data) > IMAGE_MAX_BYTES and mime != "image/jpeg":
                buf = io.BytesIO()
                im.convert("RGB").save(buf, format="JPEG", quality=85)
                data, mime = buf.getvalue(), "image/jpeg"
    return mime, data


def encode(ref: ImageRef) -> tuple[str, str] | str:
    """참조 → ``(mime, base64)``. 실패하면 그 사유 문자열 (파일 없음·해시 불일치)."""
    p = Path(ref.path)
    try:
        st = p.stat()
    except OSError:
        return "file missing"
    key = (str(p), st.st_size, st.st_mtime_ns)
    hit = _encode_cache.get(key)
    if hit is None:
        try:
            data = p.read_bytes()
        except OSError:
            return "file missing"
        sha = hashlib.sha256(data).hexdigest()
        mime = sniff_image(data[:16]) or ref.mime
        try:
            mime, data = _encode_bytes(data, mime)
        except ImageTooLarge as e:
            return str(e)
        except Exception as e:  # Pillow 가 못 여는 손상 파일 등
            return f"cannot encode ({e})"
        hit = (sha, mime, base64.b64encode(data).decode("ascii"))
        _encode_cache[key] = hit
    sha, mime, b64 = hit
    if ref.sha256 and sha != ref.sha256:
        return "file changed since it was recorded"
    return mime, b64


def expand(
    messages: list[dict],
    *,
    enabled: bool,
    part: Callable[[str, str], dict],
) -> list[dict]:
    """``images`` 를 공급자 조각으로 펼친 요청 메시지 목록.

    ``images`` 키는 **항상** 떼어 낸다 — 서버에 모르는 키를 보내지 않는다.
    ``enabled`` 면 ``content`` 가 ``[{"type":"text",…}, part(mime, b64), …]``
    가 되고, 못 읽는 참조는 조각 대신 ``content`` 끝에 사유 한 줄을 덧붙인다.
    ``enabled`` 가 아니면(텍스트 전용 모델) 조각 없이 그 사실을 한 줄 덧붙인다
    — 모델이 "봤다" 고 착각하지 않게. 레코드는 건드리지 않는다.
    """
    out: list[dict] = []
    for m in messages:
        refs = m.get("images")
        if not isinstance(refs, list) or not refs:
            if "images" in m:
                m = {k: v for k, v in m.items() if k != "images"}
            out.append(m)
            continue
        m = {k: v for k, v in m.items() if k != "images"}
        text = m.get("content")
        text = text if isinstance(text, str) else ""
        if not enabled:
            m["content"] = f"{text}\n{_NOT_SHOWN_TEXT_MODEL}".strip()
            out.append(m)
            continue
        parts: list[dict] = []
        notes: list[str] = []
        for r in refs:
            if not isinstance(r, dict):
                continue
            ref = ImageRef.from_dict(r)
            got = encode(ref)
            if isinstance(got, str):
                notes.append(f"(image {ref.path} not shown: {got})")
                continue
            parts.append(part(*got))
        if notes:
            text = "\n".join([text, *notes]).strip()
        if parts:
            m["content"] = [{"type": "text", "text": text}, *parts]
        else:
            m["content"] = text
        out.append(m)
    return out


def openai_part(mime: str, b64: str) -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def anthropic_part(mime: str, b64: str) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": mime, "data": b64},
    }


# ── 능력 프로브용 이미지 ───────────────────────────────────────────

PROBE_LEFT, PROBE_RIGHT = "green", "blue"
PROBE_QUESTION = (
    "This image has two solid colored halves. Which color is on the left and "
    "which on the right? Answer with the two color words only."
)


#: 왼쪽 green(0,160,0)·오른쪽 blue(0,0,255) 128×64 PNG (174 바이트) — Pillow 로 한 번
#: 만들어 박아 둔 상수 (v10.61.1, 프로브에 의존성이 없도록). 테스트가 색을 검증한다.
_PROBE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAIAAAABACAIAAABdtOgoAAAAdUlEQVR42u3RAQ0AMAgEsWdKkYZTpoOkp+CS"
    "Via36z29/yIAAAQAgAAAEAAAAgBAAAAIAAABACAAAAQAgAAAEAAAAgBAAAAIAAABACAAAAQAgAAAEAAAAgBA"
    "AAAIAAABACAAAAQAgAAAEAAAAgBAAAAIAADlA5BcAn/CldiJAAAAAElFTkSuQmCC"
)


def probe_image() -> tuple[str, str]:
    """``(mime, base64)`` — 왼쪽 green·오른쪽 blue 128×64 PNG.

    "이미지를 받는가" 가 아니라 "실제로 보는가" 를 묻는다: omlx 처럼 조각을
    조용히 버리는 서버에서 1×1 프로브는 거짓 양성이다(§9.5). 두 색을 순서대로
    맞혀야 하므로 텍스트 모델의 추측으로는 통과하기 어렵다.
    """
    return "image/png", _PROBE_PNG_B64


def probe_answer_ok(answer: str) -> bool:
    """프로브 답이 두 색을 **그 순서로** 담는가."""
    a = (answer or "").lower()
    i, j = a.find(PROBE_LEFT), a.find(PROBE_RIGHT)
    return i >= 0 and j >= 0 and i < j
