"""Tests for ``agent_cli.code_index.preproc``.

Coverage per DESIGN.md §12.5: each ``rewrite_*`` function gets at least
one positive (input that triggers a rewrite) and one negative
(input that should be left untouched) case; the ``preprocess_source``
end-to-end pipeline is exercised with and without ``unifdef`` flags;
``compute_preproc`` fingerprint stability and sensitivity are
asserted; ``parse_defs_file`` and ``collect_unknown_configs`` are
checked on small example inputs.

We do NOT require ``unifdef`` to be installed for these tests — when
``unifdef_flags`` is empty (or ``UNIFDEF_BIN`` is None), the pipeline
returns the rewritten text without invoking the binary, which is the
documented graceful-fallback path.
"""

from __future__ import annotations

from pathlib import Path

from agent_cli.code_index.preproc import (
    _balanced_close,
    collect_unknown_configs,
    compute_preproc,
    fold_pp_continuations,
    parse_defs_file,
    preprocess_source,
    resolve_kernel_version,
    rewrite_bare_attributes,
    rewrite_consecutive_attrs,
    rewrite_decl_macros,
    rewrite_foreach,
    rewrite_ifdef_zero,
    rewrite_type_arg_macros,
    rewrite_variadic_macros,
    strip_define_comments,
    strip_pp_trailing_ws,
)


# ----- per-rewriter unit tests ----------------------------------------------


class TestRewriteForeach:
    def test_appends_semicolon_after_for_each_macro_call(self):
        # `for_each_*` with no terminator → walker would see invalid C.
        src = "for_each_cpu(cpu, &mask) do_work(cpu)"
        out = rewrite_foreach(src)
        assert "for_each_cpu(cpu, &mask);" in out

    def test_leaves_already_terminated_call_alone(self):
        src = "for_each_cpu(cpu, &mask);"
        assert rewrite_foreach(src) == src

    def test_does_not_match_non_foreach_call(self):
        src = "some_other_call(arg)"
        assert rewrite_foreach(src) == src


class TestRewriteDeclMacros:
    def test_rewrites_declare_bitmap(self):
        out = rewrite_decl_macros("DECLARE_BITMAP(my_map, 64);")
        # Placeholder declaration replaces the macro call.
        assert "unsigned long my_map[1]" in out

    def test_rewrites_define_mutex(self):
        out = rewrite_decl_macros("DEFINE_MUTEX(my_lock);")
        assert "unsigned long my_lock[1]" in out

    def test_leaves_normal_declaration_alone(self):
        src = "int x;"
        assert rewrite_decl_macros(src) == src


class TestRewriteBareAttributes:
    def test_bare_attr_becomes_attribute_form(self):
        out = rewrite_bare_attributes("struct foo { int x; } __packed name;")
        # The bare `__packed` becomes `__attribute__((packed))`.
        assert "__attribute__((packed))" in out
        assert "__packed " not in out  # original bare form gone

    def test_function_form_bare_attr(self):
        out = rewrite_bare_attributes("int x __aligned(8);")
        assert "__attribute__((aligned(8)))" in out

    def test_already_quoted_attribute_unchanged(self):
        src = "int x __attribute__((aligned(8)));"
        # rewrite_bare_attributes only rewrites bare forms; the existing
        # `__attribute__` syntax is left alone.
        assert rewrite_bare_attributes(src) == src


class TestRewriteVariadicMacros:
    def test_named_variadic_becomes_standard(self):
        out = rewrite_variadic_macros("#define X(a, args...) f(a, args)")
        # Named-variadic `args ...` collapses to standard `...`.
        assert "#define X(a, ...)" in out

    def test_normal_macro_unchanged(self):
        src = "#define X(a, b) f(a, b)"
        assert rewrite_variadic_macros(src) == src

    def test_standard_variadic_unchanged(self):
        src = "#define X(...) f(__VA_ARGS__)"
        assert rewrite_variadic_macros(src) == src


class TestRewriteIfdefZero:
    def test_ifdef_zero_becomes_if_zero(self):
        out = rewrite_ifdef_zero("#ifdef 0\nstuff\n#endif")
        assert "#if 0" in out

    def test_ifndef_zero_becomes_if_one(self):
        # `#ifndef 0` is always-true → rewrite to `#if 1`.
        out = rewrite_ifdef_zero("#ifndef 0\nstuff\n#endif")
        assert "#if 1" in out

    def test_normal_ifdef_unchanged(self):
        src = "#ifdef CONFIG_X\nstuff\n#endif"
        assert rewrite_ifdef_zero(src) == src


class TestStripDefineComments:
    def test_block_comment_inside_define_stripped(self):
        out = strip_define_comments("#define FOO /* explanation */ 42")
        assert "/*" not in out
        assert "*/" not in out
        assert "FOO" in out and "42" in out

    def test_block_comment_outside_define_unchanged(self):
        # Comments in regular code are stripped by tree-sitter elsewhere;
        # this rewriter only touches #define lines.
        src = "int x; /* keep me */"
        assert strip_define_comments(src) == src


class TestStripPpTrailingWs:
    def test_trailing_whitespace_on_pp_directive_removed(self):
        # The rewriter operates on preprocessor lines (`#…`); a stray
        # trailing space confuses unifdef.
        out = strip_pp_trailing_ws("#define X   \n")
        assert out.endswith("X\n") or out.endswith("X")

    def test_non_pp_line_unchanged(self):
        src = "int x = 1;   "  # trailing spaces but not a pp line
        assert strip_pp_trailing_ws(src) == src


class TestRewriteConsecutiveAttrs:
    def test_two_attrs_merge(self):
        src = "int __attribute__((aligned(8))) __attribute__((packed)) x;"
        out = rewrite_consecutive_attrs(src)
        # Both attrs merged into a single __attribute__((A, B)).
        assert out.count("__attribute__") == 1
        assert "aligned(8), packed" in out or "packed, aligned(8)" in out

    def test_three_attrs_collapse_via_fixpoint(self):
        src = "int __attribute__((a)) __attribute__((b)) __attribute__((c)) x;"
        out = rewrite_consecutive_attrs(src)
        assert out.count("__attribute__") == 1

    def test_attr_between_ident_and_eq_dropped(self):
        src = "int x __attribute__((aligned(8))) = 0;"
        out = rewrite_consecutive_attrs(src)
        # The attr in pre-`=` position is removed altogether. Whitespace
        # collapses ad-hoc — assert by token presence + ordering rather
        # than exact spacing.
        assert "__attribute__" not in out
        # `x` precedes `=` with only whitespace between them.
        assert " ".join(out.split()) == "int x = 0;"

    def test_single_attr_in_safe_position_kept(self):
        src = "int __attribute__((unused)) x;"
        out = rewrite_consecutive_attrs(src)
        # Single attr in declarator-prefix position is fine — kept.
        assert "__attribute__((unused))" in out


class TestFoldPpContinuations:
    def test_backslash_continued_if_folded(self):
        src = "#if defined(A) \\\n || defined(B)\nstuff\n#endif\n"
        out = fold_pp_continuations(src)
        # The first physical line now contains the full expression.
        first = out.split("\n", 1)[0]
        assert "defined(A)" in first and "defined(B)" in first
        # Total line count preserved (continuation line replaced with blank).
        assert out.count("\n") == src.count("\n")

    def test_unrelated_text_unchanged(self):
        src = "int x = 1;\nint y = 2;\n"
        assert fold_pp_continuations(src) == src


class TestBalancedClose:
    def test_finds_matching_close(self):
        s = "abc(def(ghi)jkl)mno"
        # The opening `(` after 'abc' is at index 3, so the caller passes
        # start=4 (just after that `(`). The matching `)` is at index 15
        # and the helper returns the index AFTER it.
        assert _balanced_close(s, 4) == 16

    def test_unbalanced_returns_negative(self):
        assert _balanced_close("abc(def", 4) == -1


class TestRewriteTypeArgMacros:
    def test_container_of_strips_struct_keyword(self):
        out = rewrite_type_arg_macros("container_of(p, struct foo, link)")
        # The `struct` keyword inside the args is dropped.
        assert "struct" not in out
        assert "container_of(p, foo, link)" == out

    def test_offsetof_strips_design_dots(self):
        # `offsetof(TYPE, a.b.c)` → `offsetof(TYPE, a)`.
        out = rewrite_type_arg_macros("offsetof(my_struct, a.b.c)")
        assert "a.b.c" not in out
        assert "my_struct" in out and " a" in out

    def test_max_t_replaces_type_with_T(self):
        out = rewrite_type_arg_macros("max_t(unsigned int, x, y)")
        # Multi-word types replaced by single token `T`.
        assert "unsigned" not in out
        assert "max_t(T, x, y)" == out

    def test_va_arg_replaces_second_arg(self):
        out = rewrite_type_arg_macros("va_arg(ap, struct foo *)")
        # Second arg becomes `T`.
        assert "va_arg(ap, T)" == out

    def test_nested_parens_in_args_handled(self):
        # Manual paren balancing handles arbitrary nesting.
        out = rewrite_type_arg_macros("max_t(unsigned int, F(G(x)), y)")
        assert "max_t(T," in out
        assert "F(G(x))" in out

    def test_no_macros_present_returns_unchanged(self):
        src = "int x = compute(a, b);"
        assert rewrite_type_arg_macros(src) == src


# ----- resolve_kernel_version (called inside preprocess_source) -------------


class TestResolveKernelVersion:
    def test_kernel_version_macro_evaluated(self):
        out = resolve_kernel_version(
            "#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 10, 0)"
        )
        # `KERNEL_VERSION(a, b, c)` collapses to integer (a*65536+b*256+c).
        expected = str(5 * 65536 + 10 * 256 + 0)
        assert expected in out


# ----- preprocess_source end-to-end -----------------------------------------


class TestPreprocessSource:
    def test_no_flags_skips_unifdef_and_returns_bytes(self):
        # With empty flag list the pipeline runs all rewriters but never
        # invokes the unifdef binary — graceful fallback path.
        src = b"int x = 1;\n"
        out = preprocess_source(src, [])
        assert isinstance(out, bytes)
        # Plain code untouched by any rewriter.
        assert out == src

    def test_pipeline_applies_rewriters(self):
        # `#ifdef 0` is rewritten to `#if 0` even when unifdef isn't run
        # (no flags).
        src = b"#ifdef 0\nx\n#endif\n"
        out = preprocess_source(src, [])
        assert b"#if 0" in out

    def test_line_count_preserved(self):
        # The pipeline's invariant: file line numbers stay valid for
        # cross-referencing with the original source. fold_pp_continuations
        # blanks continuation lines rather than removing them.
        src = b"#if defined(A) \\\n || defined(B)\nstuff\n#endif\n"
        out = preprocess_source(src, [])
        assert out.count(b"\n") == src.count(b"\n")


# ----- parse_defs_file -------------------------------------------------------


class TestParseDefsFile:
    def test_define_without_value_emits_dash_d_flag(self, tmp_path):
        f = tmp_path / "defs"
        f.write_text("#define CONFIG_A\n")
        assert parse_defs_file(f) == ["-DCONFIG_A"]

    def test_define_with_value_includes_value(self, tmp_path):
        f = tmp_path / "defs"
        f.write_text("#define CONFIG_A 1\n")
        assert parse_defs_file(f) == ["-DCONFIG_A=1"]

    def test_undef_emits_dash_u(self, tmp_path):
        f = tmp_path / "defs"
        f.write_text("#undef CONFIG_B\n")
        assert parse_defs_file(f) == ["-UCONFIG_B"]

    def test_comments_and_blank_lines_skipped(self, tmp_path):
        f = tmp_path / "defs"
        f.write_text(
            "// header\n\n#define CONFIG_A 1\n// trailing\n#undef CONFIG_B  // inline\n"
        )
        assert parse_defs_file(f) == ["-DCONFIG_A=1", "-UCONFIG_B"]

    def test_missing_file_returns_empty(self, tmp_path):
        assert parse_defs_file(tmp_path / "does-not-exist") == []


# ----- collect_unknown_configs ----------------------------------------------


class TestCollectUnknownConfigs:
    def test_scans_c_h_files_for_config_names(self, tmp_path):
        (tmp_path / "a.c").write_text("#if CONFIG_FOO\nint x;\n#endif\n")
        (tmp_path / "b.h").write_text("#if CONFIG_BAR\n#endif\n")
        # explicit set contains CONFIG_FOO → not reported again.
        out = collect_unknown_configs(tmp_path, explicit={"CONFIG_FOO"})
        assert out == ["CONFIG_BAR"]

    def test_ignores_non_c_files(self, tmp_path):
        (tmp_path / "a.py").write_text("# CONFIG_FROM_PYTHON\n")
        out = collect_unknown_configs(tmp_path, explicit=set())
        assert "CONFIG_FROM_PYTHON" not in out

    def test_returns_sorted_unique(self, tmp_path):
        (tmp_path / "a.c").write_text("CONFIG_Z\nCONFIG_A\nCONFIG_M\nCONFIG_A\n")
        out = collect_unknown_configs(tmp_path, explicit=set())
        assert out == sorted(set(out))
        assert "CONFIG_A" in out
        assert "CONFIG_M" in out
        assert "CONFIG_Z" in out


# ----- compute_preproc fingerprint -------------------------------------------


class TestComputePreproc:
    def test_fingerprint_stable_for_same_inputs(self, tmp_path):
        # No defs file → flags empty → fingerprint is `sha256("schema=N|")`.
        # Calling twice should give the same hash.
        _, _, fp1 = compute_preproc(tmp_path, None, True, False)
        _, _, fp2 = compute_preproc(tmp_path, None, True, False)
        assert fp1 == fp2

    def test_fingerprint_changes_when_defs_change(self, tmp_path):
        # Build with one defs content, then a different one. Fingerprints
        # must differ.
        defs = tmp_path / "defs"
        defs.write_text("#define CONFIG_A 1\n")
        _, _, fp1 = compute_preproc(tmp_path, defs, False, False)
        defs.write_text("#define CONFIG_B 1\n")
        _, _, fp2 = compute_preproc(tmp_path, defs, False, False)
        assert fp1 != fp2

    def test_preproc_info_reflects_settings(self, tmp_path):
        defs = tmp_path / "defs"
        defs.write_text("#define CONFIG_A 1\n")
        _, info, _ = compute_preproc(tmp_path, defs, False, False)
        assert info["defs_file"] == str(defs)
        assert info["n_flags"] >= 1
        # auto_undef_count is 0 when undef_unknown_configs is False.
        assert info["auto_undef_count"] == 0

    def test_no_defs_returns_disabled_info(self, tmp_path):
        _, info, _ = compute_preproc(tmp_path, None, True, False)
        assert info["enabled"] is False
        assert info["defs_file"] is None

    def test_path_argument_can_be_missing_file(self, tmp_path):
        # parse_defs_file returns [] for a missing path; compute_preproc
        # accepts that gracefully and falls through to the no-defs branch.
        _, info, fp = compute_preproc(tmp_path, tmp_path / "missing.defs", False, False)
        assert info["enabled"] is False
        # Fingerprint identical to "no defs at all" since flags list is
        # empty in both cases.
        _, _, fp_no_defs = compute_preproc(tmp_path, None, False, False)
        assert fp == fp_no_defs


# ----- module-level sanity (no import side effects) -------------------------


def test_preproc_module_does_not_invoke_unifdef_at_import():
    # ``import agent_cli.code_index.preproc`` should not shell out — the
    # binary lookup happens once at module load via ``shutil.which`` and
    # is read-only. Spot-check by asserting UNIFDEF_BIN is a string-or-None
    # and the module is fully importable.
    import agent_cli.code_index.preproc as mod

    assert mod.UNIFDEF_BIN is None or isinstance(mod.UNIFDEF_BIN, str)
    assert Path(mod.__file__).is_file()
