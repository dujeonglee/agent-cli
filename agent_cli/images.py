"""이미지 입력 (vision, v10.59.0 — docs/vision/DESIGN.md).

레코드·``messages`` 의 ``content`` 는 끝까지 문자열이다. 이미지는 레코드의
별도 필드 ``images: [ImageRef]`` (경로·크기·해시, base64 없음) 로 실리고,
**공급자 경계** (:func:`expand`) 에서만 조각(parts)으로 펼쳐진다. 그래서
꼬리 붙이기·토큰 추정·압축·재생·세션 목록은 문자열 가정을 그대로 둔다.

기록은 참조, 내용은 보관본(v10.101.0): 참조를 만들 때 내용을 해시 이름으로
``.agent-cli/images/`` 에 복사해 두고, 보낼 때·화면에 그릴 때 그것부터 읽는다 —
같은 경로에 덮어써도 기록된 그림이 남는다. 보관본이 없으면(옛 기록) 원본을
해시로 확인해 읽고, 없거나 바뀌었으면 글 자리표시로 대신한다. 자리표시 ``[image: path (W×H)]`` 는 레코드의
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

from agent_cli import paths

#: 토큰 추정 — 픽셀 수 / 이 값. Qwen3.6(omlx) 실측 ≈ 1K px/token
#: (480×240 ≈ 130, 1280×720 ≈ 890, 1920×1080 ≈ 2,050). Anthropic 공식은
#: ≈ 750 — 차이는 서버 실측 재조정(``reconcile_actual_tokens``)이 흡수한다.
IMAGE_PX_PER_TOKEN = 1000
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
#: 참조를 만들 때 내용을 ``<sha256>.<ext>`` 로 복사해 두는 보관소 (v10.101.0,
#: ``paths.images_dir``). 같은 경로에 덮어써도 기록된 그림이 남는다 — 공급자
#: 경계(:func:`encode`)와 웹 썸네일(``/api/workspace/file?v=``)이 이것부터 읽는다.
#: 워크스페이스 밖에서 본 그림도 사본은 여기(안)로 들어온다. 테스트 seam
#: (conftest 가 테스트마다 tmp 로 돌린다).
_STORE_DIR = paths.images_dir()
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


def mentions_image(messages: list[dict]) -> bool:
    """메시지 글 어딘가에 자리표시가 있나 — 요약기 입력처럼 레코드가 한 줄
    글(``[view_image] → [image: …]``)로 풀린 뒤에도 판정한다."""
    return any(
        _PLACEHOLDER_PREFIX in m.get("content", "")
        for m in messages
        if isinstance(m.get("content"), str)
    )


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
    sha = hashlib.sha256(data).hexdigest()
    _store(sha, mime, data)
    return ImageRef(
        path=display_path(p),
        mime=mime,
        width=size[0],
        height=size[1],
        bytes=len(data),
        sha256=sha,
    )


# ── 보관소 — 기록된 그림의 사본 (v10.101.0) ─────────────────────────


def stored_path(sha: str, mime: str) -> Path:
    """보관본 자리 — ``_STORE_DIR`` 가 상대면 cwd 기준(참조의 경로와 같은 기준:
    ``display_path`` 도 cwd 상대다; 웹 서버의 workspace 는 cwd)."""
    return _STORE_DIR / f"{sha}.{IMAGE_EXT.get(mime, 'bin')}"


def _store(sha: str, mime: str, data: bytes) -> None:
    """내용 주소 사본을 남긴다 — 이미 있으면 그대로(같은 해시 = 같은 내용).

    고유한 임시 파일 + ``os.replace`` 라 읽는 쪽이 반쯤 쓴 파일을 보지 않는다 —
    같은 프로세스의 두 스레드(서브에이전트)가 같은 그림을 동시에 보관해도 서로의
    임시 파일을 자르지 않는다. 쓰지 못하면(읽기 전용 작업 폴더 등) 조용히
    넘어간다 — 그때는 종전처럼 원본 파일을 해시로 확인해 읽는다(:func:`resolve_file`).
    보관본 권한은 ``mkstemp`` 의 0600 — 읽는 쪽(하니스·웹 서버)은 같은 사용자이고,
    워크스페이스 밖에서 본 그림의 사본도 들어오므로 좁은 편이 맞다."""
    import tempfile

    dest = stored_path(sha, mime)
    if dest.is_file():
        return
    tmp_name = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, dest)
    except OSError:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def resolve_file(ref: ImageRef) -> Path | str:
    """기록된 그 내용을 담은 파일 — 보관본, 없으면 해시가 맞는 원본.

    실패하면 사유 문자열("file missing" · "file changed since it was recorded").
    보관본이 없는 건 v10.101.0 이전 기록이거나 보관에 실패한 경우다. 해시가 없는
    참조(옛 기록)는 원본을 그대로 믿는다."""
    if ref.sha256:
        # mime 을 모르면(웹 ``?v=`` — 해시만 온다) 네 확장자를 다 본다.
        mimes = (ref.mime,) if ref.mime in IMAGE_EXT else IMAGE_MIMES
        for m in mimes:
            kept = stored_path(ref.sha256, m)
            if kept.is_file():
                return kept
    p = Path(ref.path)
    if not p.is_file():
        return "file missing"
    if ref.sha256 and _file_sha(p) != ref.sha256:
        return "file changed since it was recorded"
    return p


#: ``(path, size, mtime_ns) → sha256`` — 원본 확인이 매번 파일 전체를 해시하지 않게.
_sha_cache: dict[tuple[str, int, int], str] = {}


def _file_sha(p: Path) -> str | None:
    try:
        st = p.stat()
        key = (str(p), st.st_size, st.st_mtime_ns)
        hit = _sha_cache.get(key)
        if hit is None:
            hit = hashlib.sha256(p.read_bytes()).hexdigest()
            _sha_cache[key] = hit
        return hit
    except OSError:
        return None


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


# ── 디코딩 불가 이미지 (v10.113.0, docs/vision/DESIGN.md §13) ─────────────
#
# 모델이 손수 만든 PNG(zlib·struct 로 직접 씀)가 헤더는 멀쩡하고 본문(IDAT)만
# 잘린 채 오면, 매직 바이트·헤더 크기만 보는 검사는 통과하고 서버가 400 "image_url
# does not contain a decodable image" 로 요청 전체를 거절해 런이 죽었다(벤치 40회 중
# 3회). Pillow 의 ``verify()`` 는 그 두 실물을 통과시켰다 — 본문까지 푸는 것은
# ``load()`` 뿐이다.

#: 손상 이미지 사유 뒤에 붙는 고칠 길 — 모델이 다시 만들 수 있다.
UNDECODABLE_HINT = "the file is damaged or incomplete; regenerate it"
#: 서버가 디코딩하지 못해 막은 그림의 사유 (F3 — 어느 그림인지 서버는 말하지 않는다;
#: 마지막 성공 요청에 없던 그림을 막는다).
SERVER_UNDECODABLE = f"the server could not decode it — {UNDECODABLE_HINT}"


def decode_check(data: bytes) -> str | None:
    """이미지 본문을 실제로 풀어 본다 — 못 풀면 Pillow 오류 한 줄, 풀리면 None.

    Pillow 가 없으면 None(검사 못 함 — 헤더 검사만 한 종전과 같다). ``verify()`` 가
    아니라 ``load()`` 다: 헤더가 정상이고 IDAT 가 잘린 PNG 는 ``verify()`` 를
    통과한다(§13 실측)."""
    if not has_pillow():
        return None
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
    except Exception as e:  # 손상·잘림·모르는 압축 — 무엇이든 "못 푼다"
        return (str(e).strip().splitlines() or [type(e).__name__])[0]
    return None


def is_undecodable_image_error(exc: Exception) -> bool:
    """서버가 그림을 디코딩하지 못해 요청을 거절했는가 (OpenAI 호환 경로, omlx 실측).

    ``context.overflow.classify_overflow`` 와 같은 꼴 — **HTTP 400 상태 코드가 게이트**
    이고 그 위에서 본문에 ``image_url`` 과 ``decodable`` 이 함께 있을 때만 참. 문구만
    보고 판정하지 않는다(다른 오류를 그림 탓으로 돌려 그림을 막으면 안 된다).
    Anthropic 공급자의 같은 오류 문구는 모른다 — 지어내지 않는다."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status != 400:
        return False
    text = str(exc).lower()
    return "image_url" in text and "decodable" in text


def image_shas(messages: list[dict]) -> set[str]:
    """요청 메시지들에 실린 그림 참조의 sha256 집합 (빈 값 제외)."""
    out: set[str] = set()
    for m in messages:
        refs = m.get("images")
        if not isinstance(refs, list):
            continue
        for r in refs:
            if isinstance(r, dict) and r.get("sha256"):
                out.add(str(r["sha256"]))
    return out


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


# ── 살아 있는 수 상한 — 삭제 ─────────────────────────────────────
#
# 살아 있는 이미지 수 상한(v10.59.0 ``keep_recent``·``AGENT_CLI_IMAGE_KEEP``)은
# v10.77.0 에서 지웠다 — 대화 중간의 참조를 떼면 그 뒤 전체를 다시 prefill
# 하고(KV 캐시), 말없이 빠진 그림을 모델이 다시 읽는 고리를 만들었다(GLM
# financial: 11장 → 같은 그림 다시 읽기 25회). 이미지는 붙은 레코드와 함께
# 압축이 내보낼 때만 사라지고, 그때 요약기도 그 그림을 본다
# (``strip_images`` 도 v10.77.0 에서 삭제; docs/vision/DESIGN.md §12).


# ── 공급자 경계 — 파일에서 읽어 조각으로 ─────────────────────────────

#: 세션당 ``(path, size, mtime_ns) → (sha256, mime, b64)`` — 같은 이미지가
#: 턴마다 다시 인코딩·축소되지 않게. 실패 사유(손상본·바이트 상한)도 같은 키로
#: 문자열을 담는다 (v10.113.0) — 레코드에 남은 손상본을 턴마다 다시 읽고 풀지 않게.
_encode_cache: dict[tuple[str, int, int], tuple[str, str, str] | str] = {}


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
        # 크기와 무관하게 언제나 본문을 푼다 (v10.113.0) — 종전엔 축소할 때만 load()
        # 해서, 작은 손상 PNG 가 원본 그대로 조각이 되어 서버가 요청을 400 으로
        # 거절했다. 실패는 `encode` 가 사유 줄로 바꾼다. 비용은 파일당 한 번
        # (`_encode_cache`).
        im.load()
        w, h = im.size
        if (max_edge and max(w, h) > max_edge) or len(data) > IMAGE_MAX_BYTES:
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
    """참조 → ``(mime, base64)``. 실패하면 그 사유 문자열 (파일 없음·해시 불일치).

    읽는 파일은 :func:`resolve_file` 이 고른다 — 보관본이 있으면 원본이 그 뒤에
    바뀌었어도 **기록된 그림**이 간다(v10.101.0; 종전엔 "file changed" 사유 줄로
    바뀌어 화면 기록과 어긋나고, 대화 앞부분이 바뀌어 KV 캐시도 깨졌다)."""
    got = resolve_file(ref)
    if isinstance(got, str):
        return got
    p = got
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
            hit = (sha, mime, base64.b64encode(data).decode("ascii"))
        except ImageTooLarge as e:
            hit = str(e)
        except Exception as e:  # Pillow 가 못 여는·못 푸는 손상 파일 등
            why = (str(e).strip().splitlines() or [type(e).__name__])[0]
            hit = f"cannot encode ({why}) — {UNDECODABLE_HINT}"
        _encode_cache[key] = hit
    if isinstance(hit, str):  # 캐시된 실패 사유 — 같은 파일이면 같은 사유
        return hit
    sha, mime, b64 = hit
    if ref.sha256 and sha != ref.sha256:
        return "file changed since it was recorded"
    return mime, b64


def expand(
    messages: list[dict],
    *,
    enabled: bool,
    part: Callable[[str, str], dict],
    blocked: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """``images`` 를 공급자 조각으로 펼친 요청 메시지 목록.

    ``images`` 키는 **항상** 떼어 낸다 — 서버에 모르는 키를 보내지 않는다.
    ``enabled`` 면 ``content`` 가 ``[{"type":"text",…}, part(mime, b64), …]``
    가 되고, 못 읽는 참조는 조각 대신 ``content`` 끝에 사유 한 줄을 덧붙인다.
    ``enabled`` 가 아니면(텍스트 전용 모델) 조각 없이 그 사실을 한 줄 덧붙인다
    — 모델이 "봤다" 고 착각하지 않게. 레코드는 건드리지 않는다.

    ``blocked`` (v10.113.0): 서버가 디코딩하지 못해 막은 그림의 sha256 — 그 참조는
    조각 대신 :data:`SERVER_UNDECODABLE` 사유 줄이 된다.
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
            if blocked and ref.sha256 in blocked:
                notes.append(f"(image {ref.path} not shown: {SERVER_UNDECODABLE})")
                continue
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
