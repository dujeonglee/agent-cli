"""LINUX_VERSION_CODE defaults to the newest kernel (v9.24.9).

Before, with no ``.agent-cli/defconfig`` both branches of every
``#if LINUX_VERSION_CODE …`` stayed in the parse. Two real losses came out of
that (measured on an out-of-tree Wi-Fi driver, 1146 C/H files):

* a function whose body carries a branch that opens a block in each arm
  (``#if … if (a) { #else if (b) { #endif … }``) is unbalanced for
  tree-sitter — 96 such functions were missing from the index entirely;
* the compat idiom that splits only the SIGNATURE across the branches, body
  after ``#endif``, was indexed as two bodiless declarations.

Now the newest branch is taken unless the defconfig says otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.code_index import build, load_index
from agent_cli.code_index import preproc as P

DRV_C = """\
#include <linux/version.h>

/* 1. a whole function per branch */
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(5, 10, 0))
static int setup_new(struct device *dev)
{
	return helper_new(dev);
}
#else
static int setup_old(struct device *dev)
{
	return helper_old(dev);
}
#endif

/* 2. only the signature branches, shared body */
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(6, 1, 0))
static int drv_probe(struct platform_device *pdev, const struct of_device_id *id)
#else
static int drv_probe(struct platform_device *pdev)
#endif
{
	int ret = probe_common(pdev);
	return ret;
}

/* 3. a block opened in each arm (unbalanced when both stay) */
irqreturn_t wdog_isr(int irq, void *data)
{
	int ret = 0;
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(5, 4, 0))
	if (recovery_disabled(data)) {
#else
	if (mxman_recovery_disabled()) {
#endif
		ret = 1;
	}
	return ret;
}

/* 4. an elif chain */
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 6, 0)
#define DRV_API 3
#elif LINUX_VERSION_CODE >= KERNEL_VERSION(5, 4, 0)
#define DRV_API 2
#else
#define DRV_API 1
#endif

/* 5. a plain function after all of it */
int drv_after(void)
{
	return drv_probe(0) + setup_new(0);
}
"""


def _index(root: Path, defconfig: str | None = None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "drv.c").write_text(DRV_C)
    defs = None
    if defconfig is not None:
        defs = root / ".agent-cli" / "defconfig"
        defs.parent.mkdir(parents=True, exist_ok=True)
        defs.write_text(defconfig)
    db = root / ".agent-cli" / "code_index.db"
    build(root, db, defs_path=defs, verbose=False)
    return load_index(db)


def _defs(store) -> dict[str, list[tuple[int, int, bool]]]:
    out: dict = {}
    for s in store.find_symbols(file="drv.c"):
        out.setdefault(s["name"], []).append(
            (s["line"], s["end_line"], bool(s["is_definition"]))
        )
    return out


class TestNoDefconfigTakesTheNewestBranch:
    def test_signature_split_is_one_definition_with_its_body(self, tmp_path):
        d = _defs(_index(tmp_path))
        # 18: the `#if` signature … 25: the closing brace of the shared body
        assert d["drv_probe"] == [(18, 25, True)]

    def test_block_opened_in_each_arm_no_longer_swallows_the_function(self, tmp_path):
        d = _defs(_index(tmp_path))
        assert d["wdog_isr"] == [(28, 39, True)]

    def test_only_the_newest_variant_is_indexed(self, tmp_path):
        d = _defs(_index(tmp_path))
        assert "setup_new" in d and "setup_old" not in d
        assert [r[0] for r in d["DRV_API"]] == [43]  # the 6.6 arm only

    def test_line_numbers_of_later_code_are_unchanged(self, tmp_path):
        assert _defs(_index(tmp_path))["drv_after"] == [(51, 54, True)]

    def test_the_assumption_is_recorded_in_the_index(self, tmp_path):
        info = _index(tmp_path).meta["preprocessing"]
        assert info["linux_version_code"] == P.LINUX_VERSION_CODE_DEFAULT
        assert info["linux_version_code_source"] == "default"


class TestDefconfigWins:
    @pytest.mark.parametrize(
        "value",
        ["393472", "0x060100", "KERNEL_VERSION(6, 1, 0)", "KERNEL_VERSION(6,1,0)"],
        ids=["decimal", "hex", "macro", "macro-nospace"],
    )
    def test_6_1_0_in_any_notation(self, tmp_path, value):
        """`KERNEL_VERSION(6, 1, 0)` as a value used to prune NOTHING, silently."""
        store = _index(tmp_path, f"// target\n#define LINUX_VERSION_CODE {value}\n")
        d = _defs(store)
        assert d["drv_probe"] == [(18, 25, True)]  # 6.1 ≥ 6.1 → first arm
        assert [r[0] for r in d["DRV_API"]] == [45]  # 6.1: the 5.4 ≤ v < 6.6 arm
        info = store.meta["preprocessing"]
        assert info["linux_version_code_source"] == "defconfig"
        assert info["linux_version_code"] == P.resolve_kernel_version(value)

    def test_an_old_kernel_selects_the_old_arms(self, tmp_path):
        d = _defs(
            _index(tmp_path, "#define LINUX_VERSION_CODE KERNEL_VERSION(5, 0, 0)\n")
        )
        assert "setup_old" in d and "setup_new" not in d
        assert d["drv_probe"] == [(20, 25, True)]  # the #else signature + body
        assert [r[0] for r in d["DRV_API"]] == [47]
        assert d["wdog_isr"] == [(28, 39, True)]

    def test_config_only_defconfig_still_gets_the_default_version(self, tmp_path):
        store = _index(tmp_path, "#define CONFIG_PM 1\n")
        assert store.meta["preprocessing"]["linux_version_code_source"] == "default"
        assert _defs(store)["drv_probe"] == [(18, 25, True)]

    def test_undef_keeps_both_arms_like_before(self, tmp_path):
        """`#undef LINUX_VERSION_CODE` is an explicit choice — respected, no default."""
        store = _index(tmp_path, "#undef LINUX_VERSION_CODE\n")
        info = store.meta["preprocessing"]
        assert info["linux_version_code"] == "undef"
        assert info["linux_version_code_source"] == "defconfig"


class TestUnrelatedFilesAreNotTouched:
    def test_unifdef_is_not_run_on_files_without_the_symbol(
        self, tmp_path, monkeypatch
    ):
        """The default flag must not cost a unifdef run per C file of a
        project that has nothing to do with the kernel."""
        calls = []
        monkeypatch.setattr(P, "_apply_unifdef", lambda t, f: calls.append(f) or t)
        root = tmp_path / "plain"
        root.mkdir()
        (root / "a.c").write_text(
            "#ifdef DEBUG\nint dbg;\n#endif\nint main(void) { return 0; }\n"
        )
        # `LINUX_VERSION_CODE` mentioned only outside a conditional directive
        (root / "b.c").write_text(
            "/* LINUX_VERSION_CODE */\n#define V LINUX_VERSION_CODE\nint b;\n"
        )
        build(root, root / "db", defs_path=None, verbose=False)
        assert calls == []

    def test_pure_python_backend_gives_the_same_index(self, tmp_path, monkeypatch):
        """No system `unifdef` on PATH → the bundled implementation, same result."""
        system = _defs(_index(tmp_path / "sys"))
        monkeypatch.setattr(P, "UNIFDEF_BIN", None)
        assert _defs(_index(tmp_path / "pure")) == system


class TestTheToolSurfacesIt:
    @pytest.fixture
    def proj(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "drv.c").write_text(DRV_C)
        monkeypatch.chdir(root)
        return root

    def test_build_output_names_the_assumption(self, proj):
        from agent_cli.tools.code_index import _dispatch_one

        out = _dispatch_one({"mode": "build"}).output
        assert "LINUX_VERSION_CODE assumed 0xffffff" in out
        assert ".agent-cli/defconfig" in out

    def test_build_output_with_a_defconfig_version(self, proj):
        from agent_cli.tools.code_index import _dispatch_one

        (proj / ".agent-cli").mkdir(exist_ok=True)
        (proj / ".agent-cli" / "defconfig").write_text(
            "#define LINUX_VERSION_CODE KERNEL_VERSION(6, 1, 0)\n"
        )
        out = _dispatch_one({"mode": "build"}).output
        assert "LINUX_VERSION_CODE=393472." in out and "assumed" not in out

    def test_on_demand_parse_outside_the_root_uses_the_same_default(
        self, tmp_path, monkeypatch
    ):
        from agent_cli.tools.code_index import _dispatch_one

        root = tmp_path / "root"
        root.mkdir()
        (root / "x.py").write_text("def x():\n    pass\n")
        outside = tmp_path / "elsewhere" / "drv.c"
        outside.parent.mkdir()
        outside.write_text(DRV_C)
        monkeypatch.chdir(root)
        out = _dispatch_one(
            {"mode": "fetch", "path": str(outside), "name": "drv_probe"}
        ).output
        # the NEWEST signature with the body — without the default the parser
        # happens to pair the `#else` (old) signature with the body instead
        assert out.startswith("# drv_probe (function) :18-25")
        assert "of_device_id" in out and "probe_common(pdev)" in out

    def test_the_tool_description_says_how_to_set_it(self):
        from agent_cli.tools.code_index import CodeIndexTool

        d = CodeIndexTool.description
        assert ".agent-cli/defconfig" in d and "#define LINUX_VERSION_CODE" in d
        assert "KERNEL_VERSION(6, 1, 0)" in d
