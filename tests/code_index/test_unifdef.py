"""Coverage for ``agent_cli.code_index._unifdef`` — the bundled
pure-Python ``unifdef -b`` replacement.

The harness has three layers:

1. **Unit / behavioural** — fixed input/output pairs that pin the
   semantics of every directive (#if/#ifdef/#ifndef/#elif/#else/
   #endif), of UNKNOWN propagation through &&/||, and of the ``-b``
   line-count contract. These are the contract tests; they run on
   every host.

2. **Parity vs system unifdef** — when ``shutil.which('unifdef')``
   resolves, the same inputs are re-run through the C binary and
   diffed byte-for-byte. Catches future drift between our
   implementation and the upstream tool. Skipped (not failed) when
   the binary is absent so CI hosts without it stay green.

3. **Real-world regression** — the kernel-driver case that motivated
   the whole defconfig wiring (``slsi_rx_data_ind`` signature
   straddled by ``#ifdef CONFIG_*``). Confirms that the def survives
   into the parsed output rather than disappearing into an ERROR
   node.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from agent_cli.code_index._unifdef import parse_flags, run_unifdef

SYSTEM_UNIFDEF = shutil.which("unifdef")


def _system_unifdef(text: str, flags: list[str]) -> str:
    """Run the system unifdef binary with the same flags so a parity
    test can compare byte-for-byte. Caller guarantees the binary
    exists via the ``SYSTEM_UNIFDEF`` skip guard."""
    r = subprocess.run(
        [SYSTEM_UNIFDEF, "-b", *flags],
        input=text,
        capture_output=True,
        text=True,
    )
    # Real unifdef exits 0 on "no change" and 1 on "changed"; either
    # means the stdout is authoritative. 2 means parse error — in
    # that case fall back to the original text for a sane comparison.
    return r.stdout if r.returncode != 2 else text


# ─── parse_flags ───────────────────────────────────────────


class TestParseFlags:
    """The ``-D`` / ``-U`` flag parser converts CLI-style strings into
    a flat ``{name: value}`` map. Done well it's small, but the
    edge cases (no value, integer value, opaque value, ``-U``) all
    have distinct downstream meaning so each one needs a pin."""

    def test_dash_d_without_value_defaults_to_one(self):
        # ``-DFOO`` with no ``=value`` means "defined to 1" per the
        # unifdef / cpp convention. This is the most common form
        # since defconfigs usually toggle features on rather than
        # supplying integer values.
        d = parse_flags(["-DFOO"])
        assert d.get("FOO") == 1

    def test_dash_d_with_integer_value_parses_as_int(self):
        d = parse_flags(["-DLEVEL=3"])
        assert d.get("LEVEL") == 3

    def test_dash_d_with_hex_value_parses_as_int(self):
        d = parse_flags(["-DMASK=0xFF"])
        assert d.get("MASK") == 0xFF

    def test_dash_d_with_octal_value_parses_as_int(self):
        d = parse_flags(["-DPERMS=0755"])
        assert d.get("PERMS") == 0o755

    def test_dash_d_with_int_type_suffix_stripped(self):
        # Integer literals with trailing ``L``/``UL``/``ULL`` come up
        # in real kernel defconfigs (``KERNEL_VERSION`` math). Strip
        # the suffix and store the same integer.
        d = parse_flags(["-DBIG=42UL"])
        assert d.get("BIG") == 42

    def test_dash_d_with_opaque_value_stored_as_none(self):
        # Non-numeric values can't drive arithmetic comparisons but
        # ``defined(X)`` still has to answer true — store as None to
        # mean "defined but value opaque".
        d = parse_flags(["-DNAME=somestring"])
        assert d.get("NAME") is None

    def test_dash_u_records_undefined_distinct_from_unknown(self):
        # ``-U`` must be distinguishable from "not in the table at
        # all" — the former says ``defined(X)`` is *known* to be
        # false, the latter says it's UNKNOWN.
        d = parse_flags(["-UFOO"])
        # Internal marker key — caller doesn't need to know the
        # encoding, only that the helpers see it correctly.
        assert any(k.endswith("FOO") for k in d.keys() if "\x00U\x00" in k)


# ─── Behavioural / contract tests ─────────────────────────


class TestIfdefDefined:
    """``#ifdef NAME`` keeps its body when NAME is in -D; blanks the
    body when NAME is in -U; passes the whole block through verbatim
    when NAME is neither."""

    def test_defined_keeps_body_blanks_directives(self):
        src = "#ifdef A\nkeep\n#endif\n"
        out = run_unifdef(src, ["-DA"])
        assert out == "\nkeep\n\n"

    def test_explicitly_undefined_blanks_everything(self):
        src = "#ifdef A\nkeep\n#endif\n"
        out = run_unifdef(src, ["-UA"])
        assert out == "\n\n\n"

    def test_unknown_macro_passes_through_unchanged(self):
        # Critical "do no harm" property: when we can't prove which
        # branch is live, output stays byte-identical to input so the
        # downstream parser still sees the original directives.
        src = "#ifdef A\nkeep\n#endif\n"
        out = run_unifdef(src, [])
        assert out == src

    def test_ifndef_is_inverse_of_ifdef(self):
        src = "#ifndef A\nkeep\n#endif\n"
        # -DA means A is defined → #ifndef is false → blank.
        assert run_unifdef(src, ["-DA"]) == "\n\n\n"
        # -UA → not-defined is true → keep body.
        assert run_unifdef(src, ["-UA"]) == "\nkeep\n\n"


class TestIfElseChain:
    """``#if`` / ``#elif`` / ``#else`` chains must take exactly one
    branch — and once any branch is taken, all later ``#elif``s become
    NOT_TAKEN regardless of their expression value."""

    def test_else_branch_taken_when_if_is_false(self):
        src = "#ifdef A\nA branch\n#else\nelse branch\n#endif\n"
        out = run_unifdef(src, ["-UA"])
        assert "A branch" not in out
        assert "else branch" in out
        # And line count is preserved.
        assert out.count("\n") == src.count("\n")

    def test_elif_taken_when_if_false_and_elif_true(self):
        src = (
            "#ifdef A\n"
            "A branch\n"
            "#elif defined(B)\n"
            "B branch\n"
            "#else\n"
            "else branch\n"
            "#endif\n"
        )
        out = run_unifdef(src, ["-UA", "-DB"])
        assert "A branch" not in out
        assert "B branch" in out
        assert "else branch" not in out

    def test_later_elif_skipped_once_branch_taken(self):
        # Even though ``defined(B)`` would be true, the ``-DA`` branch
        # fires first and the ``#elif`` chain stops there.
        src = "#ifdef A\nA branch\n#elif defined(B)\nB branch\n#endif\n"
        out = run_unifdef(src, ["-DA", "-DB"])
        assert "A branch" in out
        assert "B branch" not in out

    def test_unknown_elif_switches_to_pass_through(self):
        # If we can't tell whether an ``#elif`` is true and no prior
        # branch was taken, the rest of the chain has to pass through
        # — including the matching ``#endif`` — or we'd risk pruning
        # a branch that should actually fire.
        src = "#ifdef A\nA branch\n#elif defined(B)\nB branch\n#endif\n"
        out = run_unifdef(src, ["-UA"])  # A known-false, B unknown
        assert "#elif defined(B)" in out
        assert "B branch" in out


class TestExpressionEvaluator:
    """``#if EXPR`` covers the full cpp expression grammar — we only
    need a subset, but the operators we do support have to behave
    exactly. UNKNOWN propagation through short-circuit operators is
    the trickiest part."""

    def test_logical_and_with_one_false_is_false(self):
        # Short-circuit: ``0 && anything`` must evaluate to false
        # even when ``anything`` is UNKNOWN. Otherwise a perfectly
        # provable dead branch would be left as pass-through.
        src = "#if 0 && SOMETHING_UNKNOWN\ndead\n#endif\n"
        out = run_unifdef(src, [])
        assert "dead" not in out

    def test_logical_or_with_one_true_is_true(self):
        # Symmetric: ``1 || anything`` is true regardless of the
        # right side.
        src = "#if 1 || SOMETHING_UNKNOWN\nalive\n#endif\n"
        out = run_unifdef(src, [])
        assert "alive" in out

    def test_two_unknowns_in_and_stays_unknown(self):
        # No short-circuit possible → pass through.
        src = "#if defined(X) && defined(Y)\nbody\n#endif\n"
        out = run_unifdef(src, [])
        assert out == src

    def test_negated_defined(self):
        src = "#if !defined(A) && !defined(B)\nbody\n#endif\n"
        # Both undefined → both !defined are true → body kept.
        assert "body" in run_unifdef(src, ["-UA", "-UB"])
        # A defined → !defined(A) false → block dead.
        assert "body" not in run_unifdef(src, ["-DA", "-UB"])

    def test_integer_comparison(self):
        src = "#if KERNEL_VER >= 5\nmodern\n#endif\n"
        assert "modern" in run_unifdef(src, ["-DKERNEL_VER=6"])
        assert "modern" not in run_unifdef(src, ["-DKERNEL_VER=4"])

    def test_arithmetic(self):
        src = "#if (A + B) * 2 == 10\nyes\n#endif\n"
        assert "yes" in run_unifdef(src, ["-DA=2", "-DB=3"])


class TestNesting:
    """Nested ``#if`` blocks need explicit propagation: an outer
    NOT_TAKEN frame should blank everything inside (including inner
    directives), and an outer PASS_THROUGH frame should pass the
    whole nested structure through verbatim."""

    def test_outer_not_taken_blanks_inner_directives(self):
        src = "#ifdef OUTER\n  #ifdef INNER\n    inner body\n  #endif\n#endif\n"
        out = run_unifdef(src, ["-UOUTER"])
        # Every line should be blank (line count preserved).
        assert out.replace("\n", "") == ""
        assert out.count("\n") == src.count("\n")

    def test_outer_taken_inner_resolved(self):
        # OUTER taken, INNER taken too → keep innermost body.
        src = "#ifdef OUTER\n  #ifdef INNER\n    deep\n  #endif\n#endif\n"
        out = run_unifdef(src, ["-DOUTER", "-DINNER"])
        assert "deep" in out
        # Lines preserved.
        assert out.count("\n") == src.count("\n")

    def test_outer_pass_through_keeps_only_its_own_directives(self):
        # When the outer ``#if`` is UNKNOWN, only the outer's own
        # directive lines (and matching ``#endif``) stay verbatim —
        # the INNER directive is still evaluated independently. This
        # mirrors upstream unifdef: a header guard like
        # ``#ifndef __FOO_H__`` whose macro isn't on the flag list
        # shouldn't strand every CONFIG_* branch inside it as
        # un-prunable. Previously a PASS_THROUGH cascade made the
        # whole outer block pass through, which left inner directives
        # in the index for the user's wonder driver project to
        # stumble over (+236 false-positive symbols on a full rebuild).
        src = "#ifdef OUTER\n#ifdef INNER\ndeep\n#endif\n#endif\n"
        out = run_unifdef(src, ["-DINNER"])
        # Outer directives stay verbatim …
        assert out.startswith("#ifdef OUTER\n")
        assert out.rstrip().endswith("#endif")
        # … but the inner #ifdef INNER directive itself is blanked
        # (we proved INNER is defined, so the directive is noise).
        assert "#ifdef INNER" not in out
        # The body is kept because INNER evaluated true.
        assert "deep" in out
        # And the line count contract still holds.
        assert out.count("\n") == src.count("\n")

    def test_inner_evaluated_false_under_outer_pass_through(self):
        # Same shape as above but INNER is known-undefined → inner
        # body must be blanked, inner directives blanked, outer
        # directives stay verbatim. Confirms the independence cuts
        # both ways: it doesn't only "keep more", it can also "blank
        # more" inside an UNKNOWN frame.
        src = "#ifdef OUTER\n#ifdef INNER\ndead\n#endif\n#endif\n"
        out = run_unifdef(src, ["-UINNER"])
        assert out.startswith("#ifdef OUTER\n")
        assert "#ifdef INNER" not in out
        # Body of false branch is blanked, not kept.
        assert "dead" not in out
        # Outer #endif stays verbatim.
        assert out.rstrip().endswith("#endif")
        assert out.count("\n") == src.count("\n")

    def test_inner_unknown_under_outer_unknown_cascades_verbatim(self):
        # Two UNKNOWN frames stacked: nothing can be proven, so the
        # whole tree must pass through byte-identical. This is the
        # only case where PASS_THROUGH effectively cascades — because
        # the inner couldn't be resolved on its own merits, not
        # because the outer forces it.
        src = "#ifdef OUTER\n#ifdef INNER\nmaybe\n#endif\n#endif\n"
        out = run_unifdef(src, [])
        assert out == src

    def test_header_guard_pattern_does_not_strand_inner_configs(self):
        # The actual scenario that broke the wonder index: a header
        # guard wraps the entire file, and a defconfig-driven CONFIG_*
        # branch nested inside used to pass through verbatim instead
        # of being resolved. End-to-end regression so a future
        # walker change can't silently bring the bug back.
        src = (
            "#ifndef __DEBUG_H__\n"
            "#define __DEBUG_H__\n"
            "\n"
            "#ifdef CONFIG_SCSC_WLAN_DEBUG\n"
            '#define MACSTR "%02x:%02x:%02x:%02x:%02x:%02x"\n'
            "#endif\n"
            "\n"
            "#endif\n"
        )
        out = run_unifdef(src, ["-DCONFIG_SCSC_WLAN_DEBUG=1"])
        # Inner CONFIG_* directive must be blanked.
        assert "#ifdef CONFIG_SCSC_WLAN_DEBUG" not in out
        # Its body survives because CONFIG_SCSC_WLAN_DEBUG is true.
        assert 'MACSTR "%02x' in out
        # Header guard directives are kept verbatim — downstream
        # toolchain might still want them.
        assert "#ifndef __DEBUG_H__" in out
        assert "#define __DEBUG_H__" in out


class TestLineCountContract:
    """The ``-b`` flag is the whole reason this module exists: when a
    branch is pruned the line numbers of *everything after it* must
    stay identical to the input. Otherwise tree-sitter source
    positions point at the wrong code."""

    @pytest.mark.parametrize(
        "src,flags",
        [
            ("#ifdef A\nx\n#endif\n", ["-DA"]),
            ("#ifdef A\nx\n#endif\n", ["-UA"]),
            ("#if A\nx\n#elif B\ny\n#else\nz\n#endif\n", ["-DA=1"]),
            ("#if A\nx\n#elif B\ny\n#else\nz\n#endif\n", ["-DA=0", "-DB=1"]),
            ("#if A\nx\n#elif B\ny\n#else\nz\n#endif\n", ["-DA=0", "-DB=0"]),
        ],
    )
    def test_line_count_preserved(self, src, flags):
        out = run_unifdef(src, flags)
        assert out.count("\n") == src.count("\n"), (
            f"line count drift: in={src.count(chr(10))} out={out.count(chr(10))}"
        )


class TestRealWorldKernelCase:
    """The user-reported scenario: a kernel-driver function whose
    signature is split across ``#ifdef CONFIG_X`` / ``#else`` /
    ``#endif`` so that the body can't attach to either prototype
    until preprocessing collapses the chain. Without unifdef the
    whole function disappears into a tree-sitter ERROR node."""

    SRC = (
        "#ifdef CONFIG_NAPI\n"
        "void slsi_rx_data_ind(struct slsi_dev *sdev, struct net_device *dev, struct sk_buff *skb)\n"
        "#else\n"
        "static void slsi_rx_data_ind(struct slsi_dev *sdev, struct net_device *dev, struct sk_buff *skb)\n"
        "#endif\n"
        "{\n"
        "\treturn;\n"
        "}\n"
    )

    def test_napi_branch_taken(self):
        out = run_unifdef(self.SRC, ["-DCONFIG_NAPI"])
        # The "void" signature survives, the "static void" is blanked.
        assert "void slsi_rx_data_ind" in out
        assert "static void slsi_rx_data_ind" not in out
        # Body still attached.
        assert "{" in out
        assert "return;" in out

    def test_non_napi_branch_taken(self):
        out = run_unifdef(self.SRC, ["-UCONFIG_NAPI"])
        assert "static void slsi_rx_data_ind" in out
        # Trim "static void " out and check the bare "void" form
        # didn't sneak through anywhere else.
        no_static = out.replace("static void slsi_rx_data_ind", "")
        assert "void slsi_rx_data_ind" not in no_static


# ─── Parity vs system unifdef ─────────────────────────────


@pytest.mark.skipif(
    SYSTEM_UNIFDEF is None,
    reason="system unifdef not on PATH",
)
class TestParityWithSystemUnifdef:
    """Byte-for-byte comparison against the C unifdef binary.

    These tests catch behavioural drift between our reimplementation
    and the upstream tool. If a future change makes the two diverge,
    we want a focused parametrize'd failure rather than a vague
    "build pipeline output looks wrong" report from much later in the
    stack.
    """

    @pytest.mark.parametrize(
        "src,flags",
        [
            ("#ifdef A\nbody\n#endif\n", ["-DA"]),
            ("#ifdef A\nbody\n#endif\n", ["-UA"]),
            ("#ifndef A\nbody\n#endif\n", ["-DA"]),
            ("#ifndef A\nbody\n#endif\n", ["-UA"]),
            (
                "#if defined(A) && !defined(B)\nbody\n#endif\n",
                ["-DA", "-UB"],
            ),
            (
                "#if A == 1\nbody\n#endif\n",
                ["-DA=1"],
            ),
            (
                "#ifdef A\nx\n#else\ny\n#endif\n",
                ["-DA"],
            ),
            (
                "#ifdef A\nx\n#elif defined(B)\ny\n#else\nz\n#endif\n",
                ["-UA", "-DB"],
            ),
        ],
    )
    def test_matches_system_byte_for_byte(self, src, flags):
        ours = run_unifdef(src, flags)
        theirs = _system_unifdef(src, flags)
        assert ours == theirs, (
            f"divergence:\n  flags={flags}\n  ours={ours!r}\n  theirs={theirs!r}"
        )


# ─── preproc.py integration ───────────────────────────────


class TestPreprocBackendSelection:
    """The selector in ``preproc.py`` lets the operator pick a backend
    via ``AGENT_CLI_UNIFDEF`` env var (auto / system / pure). These
    tests cover the wiring — the actual transformation correctness is
    pinned by the cases above."""

    def test_pure_backend_routes_through_python_impl(self, monkeypatch):
        # Force pure mode and confirm the system binary path is not
        # invoked even when it's available. ``preprocess_source``
        # produces the same output either way for our test input, so
        # we verify the routing by patching subprocess.run to raise —
        # the pure path doesn't call it.
        import agent_cli.code_index.preproc as preproc

        monkeypatch.setattr(preproc, "_UNIFDEF_MODE", "pure")
        # If the pure path were bypassed, this raise would propagate.
        monkeypatch.setattr(
            preproc.subprocess,
            "run",
            lambda *a, **kw: pytest.fail("subprocess.run called in pure mode"),
        )

        src = b"#ifdef A\nx\n#endif\n"
        out = preproc.preprocess_source(src, ["-DA"])
        assert b"x" in out
        # Directives blanked → no leftover #ifdef in output.
        assert b"#ifdef" not in out

    def test_auto_backend_uses_system_when_available(self, monkeypatch):
        # Auto mode + system binary present → ``subprocess.run`` must
        # be the one producing output. Patch the pure-Python module to
        # fail if called, so a regression that silently skipped to
        # pure would scream.
        import agent_cli.code_index.preproc as preproc

        if preproc.UNIFDEF_BIN is None:
            pytest.skip("system unifdef not on PATH — auto mode skips by design")
        monkeypatch.setattr(preproc, "_UNIFDEF_MODE", "auto")
        monkeypatch.setattr(
            preproc._unifdef,
            "run_unifdef",
            lambda *a, **kw: pytest.fail(
                "pure-Python called in auto mode with system binary present"
            ),
        )
        src = b"#ifdef A\nx\n#endif\n"
        out = preproc.preprocess_source(src, ["-DA"])
        assert b"x" in out
