"""Workspace path confinement (default-on).

Gate the filesystem-MUTATING tools (write_file / edit_file) and shell to the
launch workspace: an op whose path resolves OUTSIDE the workspace root asks the
user for confirmation before proceeding. Reuses the same confirm / allowlist
infrastructure the dangerous-command guard uses (``renderer.confirm``,
``can_prompt``, ``interactive_lock``).

Threat model: accident prevention + a speed bump against an agent wandering out
of its workspace. This is NOT a sandbox — shell path extraction is best-effort
(it cannot see paths inside ``$(...)``, ``python -c "..."``, or shell variables);
true isolation requires an OS sandbox. ``read_file`` is intentionally NOT
workspace-gated: driver / kernel work reads toolchains and headers outside the
workspace by the dozen, so gating reads would be a prompt storm that just trains
allow-all fatigue. Only mutations and shell (which can write / exfiltrate) are
confined.

**민감 경로는 별개 축이다** (v9.10.0, `_sensitive`): 자격증명 사전에 걸리는
경로는 **읽기에도** 확인을 받는다 — `read_file` 과 `cat ~/.ssh/id_rsa` 가 같은
파일에 다르게 굴면 그건 일관성이 아니라 우연이다. 봉쇄는 워크스페이스 경계를,
사전은 "무엇을 읽느냐"를 본다. `guard(check_workspace=False)` 가 후자만 건다.

Root = the process cwd at call time, or ``AGENT_CLI_WORKSPACE_ROOT`` if set.
agent-board spawns each instance with cwd = its post workspace, so the default
lands exactly right. Disable entirely with ``AGENT_CLI_WORKSPACE_CONFINE=0``.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from agent_cli.tools._sensitive import sensitive_reason

_ENABLE_ENV = "AGENT_CLI_WORKSPACE_CONFINE"
_ROOT_OVERRIDE_ENV = "AGENT_CLI_WORKSPACE_ROOT"

# Session "always allow" roots: resolved directory subtrees the user has
# greenlit for the rest of this process (the path form of shell.py's
# keyword-based ``_session_allowlist``). A path passes the gate if it resolves
# under any allowlisted root. Cleared when the process exits.
_session_root_allowlist: set[str] = set()


def enabled() -> bool:
    """Default-on. Set ``AGENT_CLI_WORKSPACE_CONFINE=0`` to disable — for batch /
    CI runs with no human to answer, or deployments that confine another way."""
    return os.environ.get(_ENABLE_ENV, "1") != "0"


def workspace_root() -> Path:
    """The confinement root: ``AGENT_CLI_WORKSPACE_ROOT`` if set, else the
    process cwd. Resolved (``..`` collapsed, symlinks followed) so containment
    checks compare canonical paths."""
    root = os.environ.get(_ROOT_OVERRIDE_ENV) or os.getcwd()
    return Path(root).resolve()


def resolve_within(path: str, *, root: Path | None = None) -> tuple[Path, bool]:
    """Resolve ``path`` (relative → against ``root``) and report whether it lands
    inside the workspace root. ``resolve()`` collapses ``..`` and follows
    symlinks, so both traversal and symlink escapes are caught. A nonexistent
    target (writing a new file) still resolves — its would-be location is what
    gets checked.

    ``~`` 은 **셸이 확장한다** — `tool_shell` 은 ``shell=True`` 로 돌리므로
    ``cp x ~/.ssh/authorized_keys`` 는 실제 홈에 쓴다. 그런데 ``Path.resolve()``
    는 ``expanduser`` 를 하지 않아 ``<워크스페이스>/~/.ssh/...`` 로 풀렸고,
    **워크스페이스 안**으로 판정돼 게이트가 통째로 비켜갔다 (v9.10.0 수리).
    ``_path_candidate`` 가 ``~/`` 를 일부러 후보로 잡고 있었는데 그 다음 단계가
    무효화하던, 소리 없는 구멍이다."""
    r = root or workspace_root()
    p = Path(path).expanduser()
    resolved = (p if p.is_absolute() else r / p).resolve()
    inside = resolved == r or r in resolved.parents
    return resolved, inside


def _allowlisted(resolved: Path) -> bool:
    for root in _session_root_allowlist:
        rp = Path(root)
        if resolved == rp or rp in resolved.parents:
            return True
    return False


# A shell token that names a path we can meaningfully resolve against the
# workspace: an absolute path (``/…`` or ``~/…``) or an explicit ``..`` escape.
# Bare relative names (``foo.txt``, ``src/main.c``) resolve INSIDE the workspace
# so they never gate; we only pull out tokens that could point outside.
def _is_line_comment(t: str) -> bool:
    """``//`` 로 시작하는 **주석**인가 — 경로가 아니다 (v9.11.1, 사용자 제보).

    C/C++/Java/JS/Go 주석이 셸 명령에 섞이면(``echo '// note' >> x.c``,
    ``rm /tmp/x  // 주석``) 토큰이 ``/`` 로 시작해 경로 후보가 됐고, ``//`` 는
    POSIX 에서 ``/`` 로 풀려 **"워크스페이스 밖"** 확인이 떴다.

    **``//etc/passwd`` 는 제외하면 안 된다** — 셸이 실제로 ``/private/etc/passwd``
    로 풀어 쓰기 때문이다(실측). 그래서 ``//`` 접두를 통째로 버리지 않고 **둘만**
    거른다:

    1. 슬래시만 있는 토큰(``//``, ``///``) — 가리키는 대상이 없다. 맨 ``/`` 를
       후보에서 빼는 것과 같은 판단이다.
    2. ``//`` 바로 뒤가 공백 — 주석 표기이지 경로 구분자일 수 없다.

    남는 틈: ``//주석`` 처럼 공백 없이 붙여 쓴 주석은 ``//etc`` 와 문자열로
    구별되지 않아 여전히 후보다. 대부분의 주석 스타일이 ``//`` 뒤에 공백을
    두므로 실전 빈도가 낮고, 반대 방향(진짜 경로를 놓치는 것)이 더 나쁘다.
    """
    if not t.startswith("//"):
        return False
    rest = t[2:]
    return rest.strip("/") == "" or rest[:1].isspace()


def _path_candidate(tok: str) -> str | None:
    t = tok.strip("'\"").lstrip("<>")
    # --flag=/path  →  /path
    if t.startswith("--") and "=" in t:
        t = t.split("=", 1)[1]
    # short flag with attached value: -I/usr/include, -C/usr/src  →  the path.
    # A bare short flag (-rf, -C) has no path once stripped → skip.
    elif len(t) >= 2 and t[0] == "-" and t[1].isalpha():
        t = re.sub(r"^-[A-Za-z]+", "", t)
        if not t:
            return None
    if not t:
        return None
    if _is_line_comment(t):
        return None
    if t.startswith(("/", "~/")) or t == "~":
        return t
    if t == ".." or t.startswith("../") or "/../" in t:
        return t
    return None


# 경로로 보기 어려운 글자 — 정규식/awk 프로그램에 흔하고, 봉쇄할 가치가 있는
# 경로에는 사실상 안 나온다. **일부러 뺀 것**과 이유:
#   ``( )``      "Photo (1).jpg" 같은 실제 파일명
#   ``+``        /usr/include/c++/13
#   ``* ? [ ]``  글로브 — 경로를 가리키는 게 맞다
#   공백         "/Users/me/My Documents/f" (macOS 에 흔하다)
# 이 넷만으로 `/^start/…`(^) · `{print $2}`({,$) · `(users|posts)`(|) 가 걸린다.
_IMPLAUSIBLE_IN_PATH = re.compile(r"[\^$|{}]")

# 세그먼트 경계 — 배칭된 명령을 명령별로 가른다.
_SEGMENT_OPS = {"&&", "||", "|", ";", "&", "\n", "(", ")", "{", "}"}

# 리다이렉션은 **셸**이 쓴다 — 명령이 무엇이든 그 줄은 파일을 만든다.
_REDIRECT = re.compile(r"(^|[^<>])>{1,2}")

# 파일을 만들거나 지우지 **못하는** 명령. 여기 없으면 "바꿀 수 있음"으로
# 떨어져 지금까지와 똑같이 검사된다 — 목록이 불완전해도 구멍이 안 생긴다.
_READ_ONLY_CMDS = frozenset(
    (
        # 내용 보기
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "diff",
        "cmp",
        "stat",
        "file",
        "du",
        "df",
        "ps",
        "echo",
        "printf",
        "date",
        # 경로/이름 계산
        "which",
        "type",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "pwd",
        # 텍스트·바이너리 훑기
        "cut",
        "tr",
        "uniq",
        "column",
        "jq",
        "yq",
        "xxd",
        "od",
        "strings",
        "nm",
        "objdump",
        "readelf",
        # 검색 — 정규식 오탐의 진원지였다
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
    )
)

# 같은 이름이 **플래그로** 쓰기가 되는 것들 — 플래그가 있으면 읽기 판정 취소.
_WRITE_FLAGS: dict[str, frozenset[str]] = {
    "sed": frozenset({"-i", "--in-place"}),
    "sort": frozenset({"-o", "--output"}),
    "find": frozenset(
        {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fls"}
    ),
}


def _shell_segments(tokens: list[str]) -> list[list[str]]:
    """배칭된 명령줄을 명령 단위로 가른다 (``a && b | c; d``).

    ``shlex`` 는 ``&&``/``|`` 는 별도 토큰으로 주지만 ``;`` 는 앞 토큰에
    붙여 준다(``cd /tmp;``) — 둘 다 본다. 세그먼트를 잘못 나누면 곧 누락이라
    **애매하면 나누지 않는다**: 한 덩어리로 남으면 읽기 판정이 안 걸려
    모든 토큰이 후보가 된다(= 지금까지의 동작).
    """
    seg: list[str] = []
    out: list[list[str]] = []
    for tok in tokens:
        if tok in _SEGMENT_OPS:
            out.append(seg)
            seg = []
            continue
        if tok.endswith(";") and len(tok) > 1:
            seg.append(tok[:-1])
            out.append(seg)
            seg = []
            continue
        seg.append(tok)
    out.append(seg)
    return [s for s in out if s]


def _can_mutate(seg: list[str], cmd: str) -> bool:
    """이 세그먼트가 파일을 만들/지울 수 있나 — **모르면 True**.

    봉쇄는 *변경*을 막는 장치다(읽기는 `read_file` 과 같은 이유로 게이트하지
    않는다). 그래서 읽기 전용이 확실한 세그먼트는 워크스페이스 검사를 건너뛴다
    — 정규식이 경로로 오인되던 오탐이 여기서 대부분 사라진다.
    """
    if not seg:
        return False
    if _REDIRECT.search(cmd):  # 셸 리다이렉션은 명령 판정을 이긴다
        return True
    name = seg[0].rsplit("/", 1)[-1]
    flags = _WRITE_FLAGS.get(name)
    if flags is not None:
        return any(
            t in flags or any(t.startswith(f + "=") for f in flags) for t in seg[1:]
        )
    return name not in _READ_ONLY_CMDS


def extract_shell_paths(cmd: str) -> list[str]:
    """Best-effort: pull absolute paths and ``../`` escapes out of a shell
    command so :func:`guard` can check them. Deliberately narrow — it sees only
    literal path tokens, NOT paths inside ``$(...)``, ``python -c "..."``,
    variables, or globs. Those are a documented blind spot (they need an OS
    sandbox, not a string matcher); the gate is a speed bump, not a jail.

    **2단계 (v9.10.0)**: ① 세그먼트가 파일을 바꿀 수 있는지 먼저 보고
    ② 바꿀 수 있는 것에서만 경로를 뽑는다. 종전엔 ②만 있어 ``sed -n
    '/^start/,/^end/p'`` 의 **주소 정규식**이 경로로 잡혀 엉뚱한 확인을
    물었다(사용자 제보). 오탐은 allow-all 피로를 학습시켜 게이트 전체를
    무의미하게 만들므로, 이 위협 모델에서는 정밀도가 재현율보다 값지다 —
    `read_file` 을 게이트하지 않기로 한 것과 같은 판단이다.

    단 **민감 경로(`_sensitive`)는 읽기 전용 세그먼트에서도 뽑는다.** 건너뛰기가
    사전보다 먼저 걸리면 `cat ~/.ssh/id_rsa` 가 조용해진다 — 오탐을 없애려다
    유출을 여는 정반대의 실수다.
    """
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        # Unbalanced quotes — fall back to whitespace split. Better to
        # over-extract (an extra prompt) than to miss a path silently.
        tokens = cmd.split()
    found: list[str] = []
    for seg in _shell_segments(tokens):
        mutating = _can_mutate(seg, cmd)
        for tok in seg:
            cand = _path_candidate(tok)
            if cand is None or cand == "/":
                # 맨 ``/`` 는 awk ``-F/`` 나 정규식 구분자이지 대상이 아니다.
                # 루트를 진짜로 건드리는 명령은 위험 키워드 가드가 본다.
                continue
            if _IMPLAUSIBLE_IN_PATH.search(cand):
                continue
            # 읽기 전용 세그먼트라도 **민감 경로는 통과시킨다** — 안 그러면
            # `cat ~/.ssh/id_rsa` 가 조용해진다. 오탐을 없애려다 자격증명
            # 유출을 열어 주는 것이라 정반대의 실수다. 봉쇄(워크스페이스)만
            # 건너뛰고 사전은 모든 세그먼트에 건다.
            if not mutating and not sensitive_reason(cand):
                continue
            found.append(cand)
    return found


# A path-continuation char: if a path literal is FOLLOWED by one of these it is
# NOT the whole token (``/tmp/x`` inside ``/tmp/x.c``), so a highlight span needs
# a trailing boundary. No LEADING boundary is required — a flag-attached path
# (``-I/usr/inc``) glues the path to a letter and we still want the path part lit.
_PATH_BOUND = r"[^\s'\"|&;()<>`]"


def _outside_spans(command: str, literals: list[str]) -> list[tuple[int, int]]:
    """Character ranges in ``command`` of the literal path tokens that resolved
    OUTSIDE the workspace — the spans the confine confirm highlights (same
    ``command``/``danger_spans`` contract the dangerous-keyword guard uses).
    Trailing-boundary matched so a short path can't match inside a longer one;
    overlapping ranges are merged. Best-effort, mirroring the guard's tolerance —
    empty when nothing lines up (the confirm then shows the command unhighlighted
    rather than marking the wrong span)."""
    if not command:
        return []
    spans: list[tuple[int, int]] = []
    for lit in literals:
        if not lit:
            continue
        for m in re.finditer(re.escape(lit) + r"(?!" + _PATH_BOUND + r")", command):
            spans.append((m.start(), m.end()))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def guard(
    paths, action: str, *, command: str | None = None, check_workspace: bool = True
) -> str | None:
    """Gate ``paths`` for tool ``action`` (e.g. ``"write_file"``). Returns
    ``None`` when the gate is disabled, or every path is inside the workspace /
    already allowlisted — the zero-overhead common case (no renderer import, no
    lock). Otherwise prompts ONCE for the flagged paths and returns ``None`` on
    allow, or an error string on deny / when no prompt can be shown.

    **두 축을 본다** (v9.10.0):

    1. **워크스페이스 이탈** — 변경(write/edit/shell)이 루트 밖을 건드리나.
    2. **민감 경로** — 경로가 자격증명 사전(`_sensitive`)에 걸리나. 이쪽은
       *읽기에도* 적용된다: `cat ~/.ssh/id_rsa` 와 `read_file` 이 같은 파일에
       대해 다르게 굴면 그건 일관성이 아니라 우연이다.

    ``check_workspace=False`` — 축 2만 본다. `read_file` 이 이걸로 부른다:
    읽기를 봉쇄하면 커널/드라이버 작업이 밖 헤더를 수십 개 읽어 프롬프트
    폭풍이 되므로(그래서 원래 게이트 대상이 아니었다), 읽기에는 **사전만** 건다.

    ``a=always`` adds each flagged path's directory subtree to the session
    allowlist so later ops there don't re-prompt (mirrors the dangerous-command
    guard's ``a``, keyed on resolved paths instead of a keyword).

    ``command`` (shell only): the raw command line. When given, the offending
    path tokens are highlighted within it in the confirm dialog — the same
    treatment the dangerous-keyword guard gives ``rm``. Omit it (write
    /edit_file) for a prompt-only dialog."""
    if not enabled():
        return None
    root = workspace_root()
    outside: list[Path] = []
    lit_of: dict[str, str] = {}  # resolved-str → literal token (for highlighting)
    why: dict[str, str] = {}  # resolved-str → 왜 걸렸는지 (확인 창 문구)
    for path in paths:
        if not path:
            continue
        resolved, inside = resolve_within(path, root=root)
        if _allowlisted(resolved):
            continue
        secret = sensitive_reason(resolved)
        if inside and not secret:
            continue
        if not check_workspace and not secret:
            continue
        if resolved not in outside:
            outside.append(resolved)
            lit_of.setdefault(str(resolved), path)
            why[str(resolved)] = secret or "워크스페이스 밖"
    if not outside:
        return None

    from agent_cli.render import get_renderer
    from agent_cli.render.base import ConfirmOption, interactive_lock

    # Hold the shared interactive lock across the re-check + prompt: it
    # serializes against confirm/ask everywhere, and ``renderer.confirm``
    # re-acquires it internally on this same thread (re-entrant).
    with interactive_lock:
        # Re-check under the lock — another worker may have allowlisted these
        # while we waited.
        outside = [p for p in outside if not _allowlisted(p)]
        if not outside:
            return None
        renderer = get_renderer()
        joined = ", ".join(str(p) for p in outside)
        # 민감 경로가 섞여 있으면 그쪽이 헤드라인이다 — "워크스페이스 밖"보다
        # 구체적이고, 사용자가 판단하는 데 실제로 쓰이는 정보다.
        secrets = [p for p in outside if why[str(p)] != "워크스페이스 밖"]
        if not renderer.can_prompt():
            what = (
                f"reads/touches sensitive path(s) ({joined})"
                if secrets
                else f"touches path(s) outside the workspace ({joined})"
            )
            return (
                f"Refused: {action} {what} and this interface can't prompt for "
                f"confirmation right now (non-interactive shell, or no connected "
                f"client). Set AGENT_CLI_WORKSPACE_CONFINE=0 to disable these "
                f"path gates for non-interactive runs."
            )
        listing = "\n".join(f"  {p}\n    ↳ {why[str(p)]}" for p in outside)
        head = (
            "⚠ 민감 경로를 건드립니다"
            if secrets
            else f"⚠ {action} touches path(s) OUTSIDE the workspace"
        )
        prompt = (
            f"\n{head}\n"
            f"  (workspace: {root}):\n{listing}\n"
            f"Allow? (y=once, n=deny, a=always allow these locations this "
            f"session)\n  [y/n/a, optional comment after]: "
        )
        options = [
            ConfirmOption(
                key="y",
                label="once (allow this op)",
                aliases=("yes", "ok", "okay", "yep", "yeah", "sure"),
            ),
            ConfirmOption(key="n", label="deny", aliases=("no", "nope")),
            ConfirmOption(
                key="a",
                label="always allow these locations this session",
                aliases=("always", "allow"),
            ),
        ]
        # Highlight the offending path tokens inside the command (shell only).
        danger_spans = (
            _outside_spans(
                command, [lit_of[str(p)] for p in outside if str(p) in lit_of]
            )
            if command
            else None
        )
        decision, comment = renderer.confirm(
            prompt, options, default_key="n", command=command, danger_spans=danger_spans
        )
        if decision == "n":
            kind = "sensitive path" if secrets else "outside workspace"
            err = f"User denied {action} — {kind}: {joined}"
            if comment:
                err += f". User said: {comment}"
            return err
        if decision == "a":
            for p in outside:
                # Allowlist the directory subtree: a file → its parent dir, a
                # directory → itself. Sibling ops there then pass without a
                # re-prompt (the header-storm mitigation).
                d = p if p.is_dir() else p.parent
                _session_root_allowlist.add(str(d))
            # 워크스페이스 경계가 세션 내내 열린다 — 기록한다 (v9.8.0, shell 과
            # 같은 판단). 이후 그 서브트리는 재확인 없이 통과한다.
            dirs = ", ".join(
                sorted({str(p if p.is_dir() else p.parent) for p in outside})
            )
            renderer.note_next(
                f"🔓 사용자가 워크스페이스 밖 경로를 승인 — 이 세션 내내 허용: {dirs}"
            )
    return None
