# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""C/C++ preprocessor pipeline: regex rewriters + unifdef driver.

The pipeline rewrites kernel-style C constructs that confuse
tree-sitter-c (for-each macros, `container_of`, GCC attributes,
variadic-macro syntax, etc.) into shapes the grammar accepts, then
optionally runs `unifdef -b` to prune `#if` branches based on user-
supplied `-D`/`-U` flags. Line numbers are preserved end-to-end so
file:line positions in the parsed AST still match the original file.

`preprocess_source(src, unifdef_flags)` is the public entry point used
by the C and C++ language walkers (other languages use a no-op).
`compute_preproc(root, defs_path, ...)` builds the flag list, a
descriptive info dict, and a stable fingerprint string for index
invalidation.

If the `unifdef` binary is not on PATH, `preprocess_source` still
applies the regex rewriters and returns the result — only the `#if`
branch pruning is skipped (graceful fallback).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

from agent_cli.code_index import _unifdef
from agent_cli.code_index.schema import SCHEMA_VERSION

UNIFDEF_BIN = shutil.which("unifdef")

# Backend for the ``-b`` pass: prefer the system ``UNIFDEF_BIN``
# (battle-tested C implementation) when present, fall back to the
# bundled pure-Python ``_unifdef.run_unifdef`` otherwise.
KV_RE = re.compile(r"KERNEL_VERSION\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)")

# LINUX_VERSION_CODE when the defconfig does not set it (v9.24.9): the newest
# representable version (255.255.255), so every ``>= KERNEL_VERSION(...)``
# branch is taken and the index shows ONE, newest, code path. Without any
# value both branches stayed in the parse, and the common compat idiom that
# splits only a function's signature across ``#if/#else`` (body after
# ``#endif``) lost its body — the function was indexed as two bodiless
# declarations. A real version in ``.agent-cli/defconfig`` wins.
LINUX_VERSION_CODE_DEFAULT = "0xffffff"
DEFAULT_VERSION_FLAG = f"-DLINUX_VERSION_CODE={LINUX_VERSION_CODE_DEFAULT}"

_FLAG_NAME_RE = re.compile(r"-[DU]([A-Za-z_]\w*)")
# Conditional directives — the only lines ``unifdef`` evaluates symbols in.
_COND_DIRECTIVE_RE = re.compile(
    r"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|elifdef|elifndef)\b(.*)$", re.MULTILINE
)
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def with_default_version(flags: list[str]) -> list[str]:
    """``flags`` plus the default LINUX_VERSION_CODE unless the defconfig
    already defines or undefines it."""
    if any(_flag_name(f) == "LINUX_VERSION_CODE" for f in flags):
        return list(flags)
    return [*flags, DEFAULT_VERSION_FLAG]


def _flag_name(flag: str) -> str | None:
    m = _FLAG_NAME_RE.match(flag)
    return m.group(1) if m else None


def _flags_touch(text: str, flags: list[str]) -> bool:
    """Does any ``-D``/``-U`` symbol appear in a conditional directive?

    ``unifdef`` only rewrites ``#if``/``#ifdef``/``#elif``… lines whose
    expression names a given symbol; a file that names none of them comes
    back unchanged. Skipping it is therefore output-identical, and it keeps
    the default LINUX_VERSION_CODE flag from running ``unifdef`` over every
    C file of a project that has nothing to do with the kernel."""
    names = {n for n in map(_flag_name, flags) if n}
    if not names:
        return False
    for m in _COND_DIRECTIVE_RE.finditer(text):
        if names.intersection(_IDENT_RE.findall(m.group(1))):
            return True
    return False


# IS_ENABLED(X) → defined(X). unifdef can't evaluate function-like macros, but
# it does evaluate `defined()`, so this lets it prune CONFIG_* branches uniformly.
IS_ENABLED_RE = re.compile(r"IS_ENABLED\s*\(\s*([A-Za-z_]\w*)\s*\)")

# for-each loop macros: tree-sitter sees `id(args) {...}` as invalid C, and
# `id(args) stmt` (no semicolon, single-statement form) is also invalid.
# Append `;` after the macro call if not already present — that turns either
# form into valid C statement-sequence syntax. Call refs remain unchanged.
FOREACH_RE = re.compile(
    r"(\b(?:for_each_\w+|list_for_each(?:_\w+)?|hlist_for_each(?:_\w+)?|"
    r"skb_queue_walk(?:_\w+)?|netdev_for_each_\w+|xa_for_each(?:_\w+)?|"
    r"idr_for_each(?:_\w+)?|rcu_list_for_each(?:_\w+)?|"
    r"radix_tree_for_each(?:_\w+)?|llist_for_each(?:_\w+)?|"
    r"nla_for_each(?:_\w+)?|nlmsg_for_each(?:_\w+)?|skb_walk_frags)\s*"
    r"\([^()]*(?:\([^()]*\)[^()]*)*\))(?!\s*[;,)])"
)

# Type-as-argument macros: `container_of(p, struct foo, m)` is not parseable as C
# because `struct foo` isn't an expression. Strip `struct`/`union`/`enum` keywords
# inside these calls; the type name then parses as a plain identifier.
TYPE_ARG_MACROS = (
    "container_of",
    "container_of_const",
    "offsetof",
    "offsetofend",
    "FIELD_SIZEOF",
    "BUILD_BUG_ON_INVALID",
    "typeof_member",
    "list_entry",
    "list_first_entry",
    "list_first_entry_or_null",
    "list_last_entry",
    "list_next_entry",
    "list_prev_entry",
    "hlist_entry",
    "hlist_entry_safe",
    "kobj_to_dev",
    "max_t",
    "min_t",
    "clamp_t",
    "va_arg",
    "__builtin_va_arg",
)
STRUCT_KW_RE = re.compile(r"\b(struct|union|enum)\s+(\w+)")

# Declaration macros — DECLARE_BITMAP(name, size) etc. — used at file/struct
# member scope. Without expansion tree-sitter sees `call_expression;` which is
# invalid as a member declaration. Rewrite to a placeholder declaration.
DECL_MACRO_RE = re.compile(
    r"\b(DECLARE_BITMAP|DECLARE_KFIFO|DECLARE_KFIFO_PTR|DECLARE_HASHTABLE|"
    r"DECLARE_PER_CPU|DECLARE_COMPLETION|DECLARE_RWSEM|DECLARE_WAIT_QUEUE_HEAD|"
    r"DEFINE_RATELIMIT_STATE|DEFINE_MUTEX|DEFINE_SPINLOCK|DEFINE_PER_CPU|"
    r"DEFINE_STATIC_KEY_FALSE|DEFINE_STATIC_KEY_TRUE|DEFINE_IDA|DEFINE_IDR)"
    # `(name, ...balanced parens...)` — use `[^()]*` after the comma so the
    # outer `\)` only matches the macro's own closing paren, not the first
    # `)` of any nested call inside.
    r"\s*\(\s*(\w+)\s*(?:,[^()]*(?:\([^()]*\)[^()]*)*)?\)"
)

# Bare GCC attribute aliases (no __attribute__ wrapper) — `__packed`, `__aligned`,
# `__used`, etc. — confuse tree-sitter when placed between `}` and an instance
# name (`} __packed name;`). Rewrite to `__attribute__((NAME))`, which tree-sitter
# DOES parse correctly in those positions.
BARE_ATTR_RE = re.compile(
    r"\b(__packed|__used|__unused|__must_check|__deprecated|__cold|__hot|"
    r"__pure|__init|__exit|__weak|__noreturn|__force|__user|__kernel|"
    r"__iomem|__rcu|__percpu|__always_inline|__maybe_unused|__ro_after_init|"
    r"__read_mostly|__initdata|__initconst|__refdata|__visible|__always_unused)\b"
)
# Function-form variants: `__aligned(8)`, `__attribute_used__`.
BARE_ATTR_PAREN_RE = re.compile(r"\b(__aligned|__section|__alias)\s*\(([^()]*)\)")

# Variadic-macro GCC extension: `#define X(args ...)` → standard `#define X(...)`.
# tree-sitter-c doesn't parse the GCC named-rest-args syntax cleanly.
VARIADIC_MACRO_RE = re.compile(
    r"(#\s*define\s+\w+\s*\([^)]*?)\b(\w+)\s*\.\.\.\)",
    re.MULTILINE,
)

# `#ifdef 0` / `#ifndef 0` — developer typo for `#if 0`. unifdef errors on this.
IFDEF_ZERO_RE = re.compile(r"^(\s*)#\s*ifdef\s+0\b", re.MULTILINE)
IFNDEF_ZERO_RE = re.compile(r"^(\s*)#\s*ifndef\s+0\b", re.MULTILINE)

# Consecutive `__attribute__((...))` chains — tree-sitter-c rejects them
# in declaration position but accepts the merged `__attribute__((A, B))`.
CONSECUTIVE_ATTR_RE = re.compile(
    r"__attribute__\s*\(\(([^()]*(?:\([^()]*\)[^()]*)*)\)\)\s*"
    r"__attribute__\s*\(\(([^()]*(?:\([^()]*\)[^()]*)*)\)\)"
)

# tree-sitter-c rejects `name __attribute__((...)) = {...};` — attribute
# between a declarator identifier and `=`. Simplest: just drop the attribute.
ATTR_BEFORE_EQ_RE = re.compile(
    r"__attribute__\s*\(\(([^()]*(?:\([^()]*\)[^()]*)*)\)\)(\s*=)"
)

# tree-sitter-c rejects block comments INSIDE a `#define` value line.
# Strip block comments from #define lines (per line — must run after fold).
DEFINE_LINE_RE = re.compile(r"^(\s*#\s*define\s[^\n]*)$", re.MULTILINE)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# tree-sitter-c rejects trailing whitespace on `#include "..."` and other
# preprocessor lines. Trim trailing whitespace from any line starting with `#`.
PP_TRAILING_WS_RE = re.compile(r"^(\s*#[^\n]*?)[ \t]+$", re.MULTILINE)

CONFIG_RE = re.compile(r"\bCONFIG_[A-Z][A-Z0-9_]*\b")


def resolve_kernel_version(s: str) -> str:
    """Replace KERNEL_VERSION(a,b,c) macro calls with the integer they expand to.
    unifdef can't evaluate function-like macros, so we do this substitution first."""
    return KV_RE.sub(
        lambda m: str(
            (int(m.group(1)) << 16) + (int(m.group(2)) << 8) + int(m.group(3))
        ),
        s,
    )


def parse_defs_file(path: Path) -> list[str]:
    """Read a #define/#undef style config file. Return unifdef -D/-U flag list."""
    flags: list[str] = []
    if not path.is_file():
        return flags
    for raw in path.read_text().splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"#define\s+(\w+)(?:\s+(.+))?$", line)
        if m:
            name, val = m.group(1), (m.group(2) or "").strip()
            # `KERNEL_VERSION(6, 1, 0)` as a value: unifdef cannot evaluate a
            # function-like macro, so it silently pruned nothing (v9.24.9).
            val = resolve_kernel_version(val)
            flags.append(f"-D{name}={val}" if val else f"-D{name}")
            continue
        m = re.match(r"#undef\s+(\w+)$", line)
        if m:
            flags.append(f"-U{m.group(1)}")
    return flags


def collect_unknown_configs(root: Path, explicit: set[str]) -> list[str]:
    """Scan all .c/.h for CONFIG_* identifiers; return ones not in `explicit`.
    Used to auto-`#undef` config keys the user didn't list in their defs file."""
    seen: set[str] = set()
    for p in root.rglob("*"):
        if p.suffix not in (".c", ".h") or not p.is_file():
            continue
        try:
            for m in CONFIG_RE.finditer(p.read_text(errors="replace")):
                seen.add(m.group(0))
        except OSError:
            pass
    return sorted(seen - explicit)


def rewrite_foreach(text: str) -> str:
    """Insert `;` after a for-each macro call so the body parses cleanly."""
    return FOREACH_RE.sub(r"\1;", text)


def rewrite_decl_macros(text: str) -> str:
    """Replace DECLARE_BITMAP(name, ...) and friends with a placeholder
    declaration so they parse at struct-member or file scope."""
    return DECL_MACRO_RE.sub(r"unsigned long \2[1]", text)


def rewrite_bare_attributes(text: str) -> str:
    """Replace bare GCC attribute aliases with `__attribute__((...))` form.
    tree-sitter-c handles `__attribute__((X))` but not bare `__X` in many
    positions (notably between `}` and a struct instance name)."""
    text = BARE_ATTR_RE.sub(lambda m: f"__attribute__(({m.group(1).strip('_')}))", text)
    text = BARE_ATTR_PAREN_RE.sub(
        lambda m: f"__attribute__(({m.group(1).strip('_')}({m.group(2)})))", text
    )
    return text


def rewrite_variadic_macros(text: str) -> str:
    """`#define X(a, b, args ...)` → `#define X(a, b, ...)`.
    GCC named-variadic syntax confuses tree-sitter; standard `...` parses fine."""
    return VARIADIC_MACRO_RE.sub(r"\1...)", text)


def rewrite_ifdef_zero(text: str) -> str:
    """`#ifdef 0` → `#if 0` (and same for `#ifndef`). Developer typo.
    `#ifdef` needs an identifier; `0` is invalid and crashes unifdef."""
    text = IFDEF_ZERO_RE.sub(r"\1#if 0", text)
    text = IFNDEF_ZERO_RE.sub(r"\1#if 1", text)  # #ifndef 0 ≡ always true
    return text


def strip_define_comments(text: str) -> str:
    """Strip /* ... */ block comments from inside #define lines.
    tree-sitter-c rejects block comments embedded in a macro value."""

    def repl(m):
        return BLOCK_COMMENT_RE.sub(" ", m.group(1))

    return DEFINE_LINE_RE.sub(repl, text)


def strip_pp_trailing_ws(text: str) -> str:
    return PP_TRAILING_WS_RE.sub(r"\1", text)


def rewrite_consecutive_attrs(text: str) -> str:
    """Merge `__attribute__((A)) __attribute__((B))` → `__attribute__((A, B))`.
    Loop until fixed point so 3+ consecutive attributes collapse too. Then
    drop any attribute that sits between a declarator-identifier and `=`,
    since tree-sitter-c rejects that position."""
    prev = None
    while prev != text:
        prev = text
        text = CONSECUTIVE_ATTR_RE.sub(r"__attribute__((\1, \2))", text)
    text = ATTR_BEFORE_EQ_RE.sub(r"\2", text)
    return text


def fold_pp_continuations(text: str) -> str:
    """Join `\\`-continued preprocessor directives onto their first physical
    line so unifdef can evaluate them. Subsequent continuation lines become
    blank to preserve total line count.

    unifdef refuses to evaluate `#if` / `#elif` expressions split across
    multiple physical lines ("Obfuscated preprocessor control line"), which
    causes it to bail on the entire file."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    pp_re = re.compile(r"^\s*#")
    while i < len(lines):
        line = lines[i]
        if pp_re.match(line) and line.rstrip().endswith("\\"):
            parts = [line.rstrip()[:-1]]  # strip the trailing backslash
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.rstrip().endswith("\\"):
                    parts.append(nxt.rstrip()[:-1])
                    j += 1
                else:
                    parts.append(nxt)
                    break
            n_cont = j - i
            joined = " ".join(p.strip() for p in parts)
            out.append(joined)
            out.extend([""] * n_cont)
            i = j + 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


_TYPE_ARG_HEAD_RE = re.compile(r"\b(" + "|".join(TYPE_ARG_MACROS) + r")\s*\(")


def _balanced_close(text: str, start: int) -> int:
    """Given text[start-1] == '(' (open paren just consumed), return the index
    AFTER the matching ')'. -1 if unbalanced."""
    depth = 1
    i = start
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def rewrite_type_arg_macros(text: str) -> str:
    """Make container_of/offsetof/max_t/va_arg etc. parseable.

    Uses manual paren balancing rather than regex inner groups so it handles
    arbitrarily-nested arg expressions like `max_t(unsigned int,
    RPS_MAP_SIZE(cpumask_weight(mask)), L1_CACHE_BYTES)`.

    Specific fixups:
      - strip `struct`/`union`/`enum` keywords inside the args (universal)
      - `offsetof(TYPE, m.n.o)` → `offsetof(TYPE, m)` (tree-sitter rule allows
        only a single field_identifier as the designator)
      - `max_t(TYPE, ...)` → `max_t(T, ...)` (multi-word types like
        `unsigned int` aren't expressions)
      - `va_arg(ap, TYPE)` → `va_arg(ap, T)` (same reason)
    """
    type_first = {"max_t", "min_t", "clamp_t"}
    type_second = {"va_arg", "__builtin_va_arg"}
    out: list[str] = []
    i = 0
    while True:
        m = _TYPE_ARG_HEAD_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i : m.start()])
        head = m.group(1)
        end = _balanced_close(text, m.end())
        if end < 0:
            out.append(text[m.start() :])
            break
        args = text[m.end() : end - 1]
        stripped = STRUCT_KW_RE.sub(r"\2", args)
        if head == "offsetof":
            parts = stripped.split(",", 1)
            if len(parts) == 2:
                type_part, design = parts
                m2 = re.match(r"\s*([A-Za-z_]\w*)", design)
                if m2:
                    design = " " + m2.group(1)
                stripped = type_part + "," + design
        elif head in type_first:
            parts = stripped.split(",", 1)
            if len(parts) >= 2:
                stripped = "T," + parts[1]
        elif head in type_second:
            parts = stripped.split(",", 1)
            if len(parts) >= 2:
                stripped = parts[0] + ", T"
        out.append(f"{head}({stripped})")
        i = end
    return "".join(out)


def preprocess_source(src: bytes, unifdef_flags: list[str]) -> bytes:
    """Resolve KERNEL_VERSION + IS_ENABLED, rewrite kernel-isms, run unifdef -b.
    Line numbers are preserved — `unifdef -b` blanks out removed lines and our
    rewriters only replace tokens in place, so file:line positions in the
    resulting AST still match the *original* file. The original file can be
    read for human-readable slices (e.g. by a `slice` command)."""
    text = src.decode("utf-8", errors="replace")
    text = rewrite_ifdef_zero(text)
    text = rewrite_variadic_macros(text)
    text = fold_pp_continuations(text)
    text = strip_define_comments(text)
    text = strip_pp_trailing_ws(text)
    text = resolve_kernel_version(text)
    text = IS_ENABLED_RE.sub(r"defined(\1)", text)
    text = rewrite_foreach(text)
    text = rewrite_type_arg_macros(text)
    text = rewrite_decl_macros(text)
    text = rewrite_bare_attributes(text)
    text = rewrite_consecutive_attrs(text)
    if not unifdef_flags or not _flags_touch(text, unifdef_flags):
        return text.encode("utf-8")
    return _apply_unifdef(text, unifdef_flags).encode("utf-8")


def _apply_unifdef(text: str, unifdef_flags: list[str]) -> str:
    """Run ``unifdef -b`` semantics on ``text``, preferring the system
    binary and falling back to the bundled pure-Python implementation.

    The system binary path keeps the exact behaviour every existing
    install relied on; the pure-Python fallback kicks in when the binary
    is absent. If the system binary returns ``2`` (parse error on its
    end) we still try the pure-Python pass — it might handle the input
    even when the C tool gave up — before finally surrendering with the
    untouched source.
    """
    if UNIFDEF_BIN is not None:
        r = subprocess.run(
            [UNIFDEF_BIN, "-b", *unifdef_flags],
            input=text,
            capture_output=True,
            text=True,
            check=False,
        )
        # unifdef returns 0 (unchanged) or 1 (changed) on success,
        # 2 on parse error. On success the stdout is authoritative.
        if r.returncode != 2:
            return r.stdout
        # Parse error: fall through to the pure-Python implementation
        # before giving up. Lets a one-off C-tool parse error get
        # rescued by the in-process pass without losing the prune.
    return _unifdef.run_unifdef(text, unifdef_flags)


def compute_preproc(
    root: Path,
    defs_path: Path | None,
    undef_unknown_configs: bool,
    verbose: bool,
):
    """Return (unifdef_flags, preproc_info, preproc_fingerprint).

    The flags always carry a LINUX_VERSION_CODE (v9.24.9): the defconfig's,
    else :data:`LINUX_VERSION_CODE_DEFAULT` (newest). ``preproc_info``
    reports which one — ``linux_version_code`` and
    ``linux_version_code_source`` (``defconfig`` | ``default``) — so the
    assumption is visible to whoever reads the index."""
    unifdef_flags: list[str] = []
    preproc_info = {
        "enabled": False,
        "defs_file": None,
        "unifdef_bin": None,
        "n_flags": 0,
        "auto_undef_count": 0,
    }
    if defs_path is not None and defs_path.is_file():
        unifdef_flags = parse_defs_file(defs_path)
        auto_undef_count = 0
        if undef_unknown_configs:
            explicit = {
                re.match(r"-[DU](\w+)", f).group(1)
                for f in unifdef_flags
                if re.match(r"-[DU](\w+)", f)
            }
            extras = collect_unknown_configs(root, explicit)
            unifdef_flags.extend(f"-U{k}" for k in extras)
            auto_undef_count = len(extras)
        preproc_info["defs_file"] = str(defs_path)
        preproc_info["auto_undef_count"] = auto_undef_count
    source = (
        "defconfig"
        if any(_flag_name(f) == "LINUX_VERSION_CODE" for f in unifdef_flags)
        else "default"
    )
    unifdef_flags = with_default_version(unifdef_flags)
    lvc = next((f for f in unifdef_flags if _flag_name(f) == "LINUX_VERSION_CODE"), "")
    # The pure-Python fallback is always available, so the presence/absence
    # of the system binary no longer gates whether the preproc pass runs.
    # ``unifdef_bin`` still reports the system binary path (for diagnostics /
    # verbose output) but it's no longer a hard prerequisite.
    backend = "system" if UNIFDEF_BIN is not None else "pure"
    preproc_info.update(
        {
            "enabled": True,
            "unifdef_bin": UNIFDEF_BIN,
            "backend": backend,
            "n_flags": len(unifdef_flags),
            # `-DLINUX_VERSION_CODE=v` → v ; `-ULINUX_VERSION_CODE` → "undef"
            "linux_version_code": lvc.split("=", 1)[1] if "=" in lvc else "undef",
            "linux_version_code_source": source,
        }
    )
    if verbose:
        via = UNIFDEF_BIN if backend == "system" else "_unifdef.py (pure-Python)"
        where = defs_path if preproc_info["defs_file"] else "(no defconfig)"
        msg = f"  [preproc] {where} → {len(unifdef_flags)} flag(s) via {via}"
        if preproc_info["auto_undef_count"]:
            msg += f"  (incl. {preproc_info['auto_undef_count']} auto-undef CONFIG_*)"
        msg += f"  LINUX_VERSION_CODE={preproc_info['linux_version_code']} ({source})"
        print(msg, file=sys.stderr)

    # Fingerprint: anything that would make old parsed data incompatible.
    h = hashlib.sha256()
    h.update(f"schema={SCHEMA_VERSION}|".encode())
    h.update(("|".join(sorted(unifdef_flags))).encode())
    return unifdef_flags, preproc_info, h.hexdigest()
