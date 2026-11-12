"""``agent-cli stats`` — 본문 없는 집계 내보내기 (v10.130.0).

존재 이유가 개인정보 계약이다: 여는 파일은 ``turns.jsonl`` 뿐, 출력의 문자열은 닫힌
어휘(버전·방언·내장 도구·고정 라벨)와 모델 이름뿐. 감시 문자열을 기록 곳곳에 심고
표·JSON·``--out`` 파일 어디에도 나오지 않는지 본다.
"""

from __future__ import annotations

import builtins
import inspect
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_cli import __version__
from agent_cli.context import manager as _manager
from agent_cli.main import app
from agent_cli.recovery import stats as _stats
from agent_cli.recovery.observability import OUTPUT_CAP_KINDS, classify_output_cap
from agent_cli.recovery.stats import collect, format_table

SECRET = "SENTINEL_7a1e"


def _turn(**kw) -> dict:
    row = {
        "model": "qwen-35b",
        "timestamp": "2026-11-10T03:00:00+00:00",
        "parse_stage": 1,
        "failure_signal": None,
        "primitives_applied": [],
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "stop_reason": None,
        "stop_detail": None,
        "harness_version": "10.130.0",
        "dialect": "json_fc",
    }
    row.update(kw)
    return row


def _write(path: Path, rows: list, raw: list[str] = ()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in rows] + list(raw)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _invoke(*args, env_home: Path):
    return CliRunner().invoke(app, ["stats", *args], env={"HOME": str(env_home)})


def _only_group(data: dict) -> dict:
    [g] = data["groups"]
    return g


# ── 개인정보 계약 ──────────────────────────────────────


@pytest.fixture
def planted(tmp_path) -> Path:
    """감시 문자열이 곳곳에 심긴 세션 트리."""
    root = tmp_path / "root"
    sess = root / f"{SECRET}_session" / "abc123"
    _write(sess / "history.jsonl", [{"role": "user", "content": SECRET}])
    _write(sess / "failures.jsonl", [{"text": SECRET, "stop_detail": SECRET}])
    (sess / "session.json").write_text(json.dumps({"title": SECRET}))
    _write(
        sess / "turns.jsonl",
        [
            _turn(
                stop_reason="server_error",
                stop_detail=f"server_error: {SECRET} at /Users/{SECRET}/x",
                failure_signal="NO_OUTPUT",
                cwd=f"/Users/{SECRET}/proj",
                title=SECRET,
                session_id=SECRET,
            ),
            _turn(
                failure_signal=SECRET,
                tools=[
                    ["read_file", True, 3],
                    [f"mcp__{SECRET}__q", False, 9],
                    [SECRET, True, 1],
                ],
                primitives_applied=[f"foreign_parse:{SECRET}"],
            ),
            _turn(dialect=SECRET, harness_version=SECRET, parse_stage=SECRET),
            _turn(stop_reason=SECRET, stop_detail=SECRET),
            {
                "event": "compaction",
                "timestamp": "2026-11-10T04:00:00+00:00",
                "harness_version": "10.130.0",
                "dialect": "json_fc",
                "tokens_before": 10,
                "tokens_after": 5,
                "summary_shape": SECRET,
                "shape_fallback": f"error: {SECRET}",
                "failure_signal": SECRET,
                "fallback_used": False,
                "duration_ms": 1.0,
            },
            {
                "event": "compaction",
                "timestamp": "2026-11-10T04:00:00+00:00",
                "harness_version": "10.130.0",
                "dialect": "json_fc",
                "shape_fallback": f"{SECRET}: x",
            },
            {"event": SECRET, "timestamp": "2026-11-10T04:00:00+00:00"},
        ],
    )
    return root


class TestPrivacy:
    def test_no_planted_string_in_table_or_json(self, planted, tmp_path):
        data = collect([planted])
        assert data["files_read"] == 1
        assert SECRET not in json.dumps(data)
        assert SECRET not in format_table(data)
        # 어휘 밖 값은 other 로 묶였다
        groups = {(g["harness_version"], g["dialect"]): g for g in data["groups"]}
        assert ("other", "other") in groups
        main = groups[("10.130.0", "json_fc")]
        assert set(main["tools"]) == {"read_file", "other"}
        assert main["tools"]["other"]["calls"] == 2
        assert main["results"]["failure_signal"]["other"] == 1
        assert main["results"]["stop_detail"] == {"other": 2}
        assert main["results"]["stop_reason"]["other"] == 1
        assert main["compaction"]["summary_shape"]["other"] == 1
        assert main["compaction"]["shape_fallback"] == {"error": 1, "other": 1}
        assert data["lines_skipped"] == 1  # 모르는 event

    def test_cli_outputs_and_out_file_hold_no_planted_string(self, planted, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        for args in ([], ["--json"]):
            r = _invoke(str(planted), *args, env_home=home)
            assert r.exit_code == 0, r.output
            assert SECRET not in r.output
        out = tmp_path / "out.json"
        r = _invoke(str(planted), "--json", "--out", str(out), env_home=home)
        assert r.exit_code == 0
        assert SECRET not in out.read_text()
        assert json.loads(out.read_text())["schema"] == 1

    def test_opens_only_turns_jsonl(self, planted, monkeypatch):
        opened: list[str] = []
        real_open = builtins.open

        def spy(file, *a, **kw):
            opened.append(Path(file).name)
            return real_open(file, *a, **kw)

        monkeypatch.setattr(builtins, "open", spy)
        collect([planted])
        assert opened and set(opened) == {"turns.jsonl"}

    def test_unreadable_neighbours_do_not_matter(self, planted):
        """turns.jsonl 아닌 파일을 열면 실패한다 — 읽기 권한을 지운다."""
        sess = next(planted.rglob("history.jsonl")).parent
        locked = [
            sess / "history.jsonl",
            sess / "failures.jsonl",
            sess / "session.json",
        ]
        for p in locked:
            p.chmod(0)
        try:
            data = collect([planted])
        finally:
            for p in locked:
                p.chmod(0o600)
        assert data["files_read"] == 1

    def test_header_says_what_is_inside(self, planted):
        data = collect([planted])
        assert data["contains"] == _stats.CONTAINS
        assert format_table(data).splitlines()[1] == _stats.CONTAINS

    def test_symlinks_are_not_followed(self, tmp_path):
        outside = tmp_path / "outside"
        _write(outside / "s" / "turns.jsonl", [_turn()])
        root = tmp_path / "root"
        root.mkdir()
        (root / "linked_dir").symlink_to(outside)
        (root / "own").mkdir()
        (root / "own" / "turns.jsonl").symlink_to(outside / "s" / "turns.jsonl")
        assert collect([root])["files_read"] == 0
        # 링크 PATH 자체를 줘도 따라가지 않는다
        assert collect([root / "linked_dir"])["files_read"] == 0

    # ── 집계 ──────────────────────────────────────────────

    @pytest.mark.parametrize(
        "served",
        [
            f"/Users/{SECRET}/models/m",
            f"~/{SECRET}/m",
            f"./{SECRET}/m/",
            f"../{SECRET}/m",
            f"C:\\{SECRET}\\models\\m",
            f"D:/{SECRET}/m",
        ],
    )
    def test_path_shaped_model_keeps_only_the_last_part(self, tmp_path, served):
        """vLLM 은 --model 경로를 서빙 이름으로 쓴다 — 경로는 내지 않는다."""
        _write(tmp_path / "turns.jsonl", [_turn(model=served)])
        data = collect([tmp_path])
        assert SECRET not in json.dumps(data) and SECRET not in format_table(data)
        assert _only_group(data)["model"] == "m"

    @pytest.mark.parametrize(
        "served, shown",
        [
            ("Qwen/Qwen3-35B-A3B", "Qwen/Qwen3-35B-A3B"),
            ("glm\n 5.3\t flash", "glm 5.3 flash"),
            ("x" * 300, "x" * 120),
            ("   ", "unknown"),
            ("/", "unknown"),
        ],
    )
    def test_model_name_is_one_short_line(self, tmp_path, served, shown):
        _write(tmp_path / "turns.jsonl", [_turn(model=served)])
        assert _only_group(collect([tmp_path]))["model"] == shown


class TestAggregate:
    def test_tokens_time_and_n(self, tmp_path):
        _write(
            tmp_path / "a" / "turns.jsonl",
            [
                _turn(
                    input_tokens=100,
                    cache_read_input_tokens=900,
                    output_tokens=50,
                    reasoning_tokens=10,
                    ttft_ms=200,
                    llm_ms=1000,
                    tool_ms=250,
                    tools=[["shell", True, 200], ["shell", False, 40]],
                ),
                # 옛 모양(새 필드 없음) — 토큰 합에는 들고 추론·시간 분모에서는 빠진다
                _turn(input_tokens=1000, output_tokens=30),
            ],
        )
        _write(
            tmp_path / "b" / "turns.jsonl",
            [_turn(llm_ms=3000, tool_ms=750, stop_reason="length")],
        )
        g = _only_group(collect([tmp_path]))
        assert (g["sessions"], g["turns"]) == (2, 3)
        assert g["turns_per_session"] == {"median": 1, "p90": 2, "max": 2}
        tok = g["tokens"]
        assert (tok["n"], tok["input"], tok["output"], tok["cache_read"]) == (
            3,
            2100,
            100,
            900,
        )
        assert tok["cache_hit_rate"] == pytest.approx(900 / 2100)
        assert tok["reasoning"] == {"n": 1, "sum": 10, "ratio": pytest.approx(0.2)}
        t = g["time_ms"]
        assert t["ttft"]["n"] == 1
        assert (t["llm"]["n"], t["llm"]["sum"], t["llm"]["median"]) == (2, 4000, 1000)
        assert t["llm"]["p90"] == 3000
        assert t["llm_to_tool"] == pytest.approx(4.0)
        assert g["tools"]["shell"] == {
            "calls": 2,
            "failures": 1,
            "ms_sum": 240,
            "ms_median": 40,
        }
        assert g["results"]["cut_turns"] == 1
        assert g["results"]["stop_reason"] == {"length": 1, "none": 2}
        assert (g["first_day"], g["last_day"]) == ("2026-11-10", "2026-11-10")

    def test_old_rows_group_as_unknown(self, tmp_path):
        row = _turn()
        del row["harness_version"], row["dialect"]
        _write(tmp_path / "turns.jsonl", [row])
        g = _only_group(collect([tmp_path]))
        assert (g["harness_version"], g["model"], g["dialect"]) == (
            "unknown",
            "qwen-35b",
            "unknown",
        )

    def test_groups_split_by_version_model_dialect(self, tmp_path):
        _write(
            tmp_path / "turns.jsonl",
            [
                _turn(),
                _turn(harness_version="10.129.0"),
                _turn(model="glm"),
                _turn(dialect="native_fc"),
            ],
        )
        assert len(collect([tmp_path])["groups"]) == 4

    def test_compaction_takes_the_model_of_the_turn_before(self, tmp_path):
        _write(
            tmp_path / "turns.jsonl",
            [
                {"event": "compaction", "tokens_before": 1, "tokens_after": 1},
                _turn(model="glm"),
                {
                    "event": "compaction",
                    "harness_version": "10.130.0",
                    "dialect": "json_fc",
                    "tokens_before": 9000,
                    "tokens_after": 3000,
                    "duration_ms": 1200.0,
                    "summary_shape": "transcript",
                    "shape_fallback": "too_large: ~9000 > 100 tokens",
                    "fallback_used": True,
                },
            ],
        )
        groups = {g["model"]: g for g in collect([tmp_path])["groups"]}
        c = groups["glm"]["compaction"]
        assert c["count"] == 1
        assert (c["tokens_before_median"], c["tokens_after_median"]) == (9000, 3000)
        assert c["summary_shape"] == {"transcript": 1}
        assert c["shape_fallback"] == {"too_large": 1}
        assert c["fallback_used"] == 1
        assert groups["unknown"]["compaction"]["count"] == 1

    def test_compaction_after_a_filtered_turn_keeps_its_model(self, tmp_path):
        """--since 로 걸러진 턴 뒤의 압축 줄도 그 턴의 모델 묶음으로 간다."""
        _write(
            tmp_path / "turns.jsonl",
            [
                _turn(model="glm", timestamp="2026-11-01T00:00:00+00:00"),
                {
                    "event": "compaction",
                    "timestamp": "2026-11-12T00:00:00+00:00",
                    "harness_version": "10.130.0",
                    "dialect": "json_fc",
                    "tokens_before": 9000,
                    "tokens_after": 3000,
                },
            ],
        )
        data = collect([tmp_path], since="2026-11-12")
        g = _only_group(data)
        assert g["model"] == "glm" and g["turns"] == 0
        assert g["compaction"]["count"] == 1

    def test_non_finite_numbers_are_not_counted(self, tmp_path):
        """json.loads 는 Infinity·NaN 을 받는다 — 합이 Infinity 가 되지 않는다."""
        _write(
            tmp_path / "turns.jsonl",
            [_turn(llm_ms=100)],
            raw=[
                json.dumps(_turn(llm_ms=1)).replace(
                    '"llm_ms": 1', '"llm_ms": Infinity'
                ),
                json.dumps(_turn(llm_ms=1)).replace('"llm_ms": 1', '"llm_ms": NaN'),
                json.dumps(_turn()).replace(
                    '"input_tokens": 100', '"input_tokens": -Infinity'
                ),
            ],
        )
        data = collect([tmp_path])
        out = json.dumps(data)
        assert "Infinity" not in out and "NaN" not in out
        table = format_table(data)
        assert "inf" not in table and "nan" not in table
        g = _only_group(data)
        assert g["turns"] == 4
        assert g["time_ms"]["llm"]["n"] == 1 and g["time_ms"]["llm"]["sum"] == 100

    def test_broken_lines_are_skipped_and_counted(self, tmp_path):
        _write(tmp_path / "turns.jsonl", [_turn()], raw=["{not json", "[1, 2]", ""])
        data = collect([tmp_path])
        assert (data["lines_read"], data["lines_skipped"]) == (3, 2)
        assert _only_group(data)["turns"] == 1

    def test_since_filters_by_day(self, tmp_path):
        _write(
            tmp_path / "turns.jsonl",
            [
                _turn(timestamp="2026-11-01T00:00:00+00:00"),
                _turn(timestamp="2026-11-12T23:59:00+00:00"),
                _turn(timestamp=None),
            ],
        )
        data = collect([tmp_path], since="2026-11-12")
        assert data["lines_filtered"] == 2
        assert _only_group(data)["turns"] == 1

    def test_overlapping_paths_count_a_file_once(self, tmp_path):
        f = _write(tmp_path / "s" / "turns.jsonl", [_turn()])
        assert collect([tmp_path, tmp_path / "s", f])["files_read"] == 1

    def test_finds_bench_job_sessions(self, tmp_path):
        """벤치 job 디렉터리 — 어댑터가 trial 마다 logs/agent/sessions 에 남긴다."""
        job = tmp_path / "job"
        for trial in ("t1__abc", "t2__def"):
            sess = job / trial / "logs" / "agent" / "sessions" / "s1"
            _write(sess / "turns.jsonl", [_turn(), _turn()])
            _write(sess / "history.jsonl", [{"content": SECRET}])
        data = collect([job])
        assert data["files_read"] == 2
        assert _only_group(data)["turns"] == 4


# ── 닫힌 어휘의 출처 ───────────────────────────────────


class TestVocabulary:
    def test_output_cap_kinds_cover_classifier(self):
        outs = {
            classify_output_cap(o, r, c)
            for o in (None, 0, 10, 100)
            for r in (None, 0, 10)
            for c in (True, False)
        }
        assert outs <= set(OUTPUT_CAP_KINDS)

    def test_shape_fallback_kinds_cover_manager_reasons(self):
        src = inspect.getsource(_manager.ContextManager._try_boundary)
        reasons = set(re.findall(r'return None, (?:_short_reason\()?f?"([a-z_]+)', src))
        assert reasons and reasons <= set(_manager.SHAPE_FALLBACK_KINDS)

    def test_summary_shapes_cover_manager(self):
        src = inspect.getsource(_manager.ContextManager._summarize_evicted)
        assert set(re.findall(r'info\["shape"\] = "([a-z_]+)"', src)) == set(
            _manager.SUMMARY_SHAPES
        )

    def test_builtin_tools_and_dialects_pass_by_name(self, tmp_path):
        _write(
            tmp_path / "turns.jsonl",
            [_turn(dialect="xml_fc", tools=[["edit_file", True, 1]])],
        )
        g = _only_group(collect([tmp_path]))
        assert g["dialect"] == "xml_fc" and set(g["tools"]) == {"edit_file"}


# ── 명령 ──────────────────────────────────────────────


class TestCommand:
    def test_needs_no_setup_or_provider(self, tmp_path, monkeypatch):
        import agent_cli.main as main_mod

        def boom(*_a, **_k):
            raise AssertionError("stats must not boot")

        monkeypatch.setattr(main_mod, "_maybe_setup", boom)
        monkeypatch.setattr(main_mod, "create_provider", boom)
        _write(tmp_path / "s" / "turns.jsonl", [_turn()])
        home = tmp_path / "home"
        home.mkdir()
        r = _invoke(str(tmp_path / "s"), env_home=home)
        assert r.exit_code == 0, r.output
        assert "qwen-35b" in r.output
        assert not (home / ".agent-cli").exists()

    def test_default_path_is_sessions_dir(self, tmp_path, monkeypatch):
        sess = tmp_path / "sessions"
        _write(sess / "x" / "turns.jsonl", [_turn()])
        monkeypatch.setenv("AGENT_CLI_SESSIONS_DIR", str(sess))
        r = _invoke("--json", env_home=tmp_path)
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["files_read"] == 1 and data["harness_version"] == __version__

    def test_empty_tree_is_exit_zero(self, tmp_path):
        r = _invoke(str(tmp_path), env_home=tmp_path)
        assert r.exit_code == 0
        assert "No turn records found." in r.output

    @pytest.mark.parametrize("since", ["2026-13-01", "20261112", "yesterday"])
    def test_bad_since_is_exit_two(self, tmp_path, since):
        r = _invoke(str(tmp_path), "--since", since, env_home=tmp_path)
        assert r.exit_code == 2

    def test_missing_path_is_exit_two(self, tmp_path):
        r = _invoke(str(tmp_path / "nope"), env_home=tmp_path)
        assert r.exit_code == 2
        assert "No such path" in r.output

    @pytest.mark.parametrize("target", ["dir", "file"])
    def test_symlinked_path_argument_is_exit_two(self, tmp_path, target):
        """직접 준 PATH 가 링크면 조용히 0건이 아니라 사실을 말한다."""
        real = _write(tmp_path / "real" / "turns.jsonl", [_turn()])
        link = tmp_path / "link"
        link.symlink_to(real.parent if target == "dir" else real)
        r = _invoke(str(link), env_home=tmp_path)
        assert r.exit_code == 2
        assert "Not following a symbolic link" in r.output
        assert str(link) in r.output.replace("\n", "")

    def test_symlink_inside_the_tree_is_skipped_quietly(self, tmp_path):
        real = _write(tmp_path / "outside" / "turns.jsonl", [_turn()])
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "sess").symlink_to(real.parent)
        r = _invoke(str(tree), env_home=tmp_path)
        assert r.exit_code == 0
        assert "No turn records found." in r.output

    @pytest.mark.parametrize("where", ["missing_dir", "read_only"])
    def test_out_write_failure_is_one_line_exit_two(self, tmp_path, where):
        _write(tmp_path / "s" / "turns.jsonl", [_turn()])
        if where == "missing_dir":
            out = tmp_path / "nope" / "stats.json"
        else:
            ro = tmp_path / "ro"
            ro.mkdir()
            ro.chmod(0o500)
            out = ro / "stats.json"
        try:
            r = _invoke(str(tmp_path / "s"), "--out", str(out), env_home=tmp_path)
        finally:
            if where == "read_only":
                (tmp_path / "ro").chmod(0o700)
        assert r.exit_code == 2
        assert r.exception is None or isinstance(r.exception, SystemExit)
        assert "Traceback" not in r.output
        assert r.output.startswith("Cannot write ")
        assert not out.exists()

    def test_out_line_keeps_the_path_whole(self, tmp_path, monkeypatch):
        """경로 줄은 80칸에서 끊기지 않는다(복사해 쓰게)."""
        from agent_cli.render import console

        monkeypatch.setattr(console, "width", 80)
        deep = tmp_path / ("d" * 70) / "stats-output.json"
        deep.parent.mkdir()
        _write(tmp_path / "s" / "turns.jsonl", [_turn()])
        r = _invoke(str(tmp_path / "s"), "--out", str(deep), env_home=tmp_path)
        assert r.exit_code == 0, r.output
        assert str(deep) in r.output.replace("\n", "") and str(deep) in r.output
