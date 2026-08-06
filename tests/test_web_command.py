"""``agent-cli web`` helpers — browser auto-open gating.

The browser is auto-opened only for a local-machine bind (loopback or the
wildcards, reachable at localhost on the same box). A specific non-loopback
``--host <ip>`` is a remote bind: the operator browses from elsewhere, so
auto-opening a browser on the server is useless and was reported as annoying.
"""

from __future__ import annotations

from agent_cli.main import _is_local_bind


class TestIsLocalBind:
    def test_wildcards_are_local(self):
        # default `agent-cli web` binds 0.0.0.0 on your own machine → open
        assert _is_local_bind("0.0.0.0")
        assert _is_local_bind("::")

    def test_loopback_is_local(self):
        assert _is_local_bind("127.0.0.1")
        assert _is_local_bind("localhost")
        assert _is_local_bind("::1")
        assert _is_local_bind("LocalHost")  # case-insensitive
        assert _is_local_bind(" 127.0.0.1 ")  # tolerant of stray spaces

    def test_specific_ip_is_remote(self):
        # remote bind → do NOT auto-open
        assert not _is_local_bind("192.168.1.5")
        assert not _is_local_bind("10.0.0.3")
        assert not _is_local_bind("203.0.113.7")


class TestPickPort:
    """``pick_port`` prefers the requested port, else falls back to a free one
    (the dynamic collision avoidance the web command relies on)."""

    def test_free_preferred_is_used(self):
        import socket

        from agent_cli.web.server import pick_port

        # find a currently-free port, then ask pick_port for it
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        assert pick_port("127.0.0.1", free) == free

    def test_busy_preferred_falls_back(self):
        import socket

        from agent_cli.web.server import pick_port

        # hold a port with a LIVE listener, then ask for it → must differ
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        busy = srv.getsockname()[1]
        try:
            got = pick_port("127.0.0.1", busy)
            assert got != busy and got > 0  # OS-assigned fallback
        finally:
            srv.close()


# ── C4: run/web 공용 부트스트랩 + --resume 공용 경로 + 예산 통일 ──────


class TestC4Bootstrap:
    def _fake_setup(self, monkeypatch, context_window=100_000):
        import agent_cli.main as m

        class _Caps:
            pass

        caps = _Caps()
        caps.context_window = context_window
        caps.max_output_tokens = 8_192
        monkeypatch.setattr(
            m,
            "_setup_provider",
            lambda *a, **k: ("PROV", caps, "m1", "http://u", "k", "openai"),
        )
        return m

    def test_bootstrap_bundles_and_budget_fallback(self, monkeypatch):
        m = self._fake_setup(monkeypatch)
        boot = m._bootstrap_provider(None, None, "", "", "json_fc", 0)
        assert boot.resolved_model == "m1" and boot.provider_name == "openai"
        assert boot.wire_format.name == "json_fc"
        # 예산 폴백 = 70% 통일 공식 (run/web 동일)
        assert boot.max_context_tokens == (100_000 * 7) // 10

    def test_bootstrap_explicit_budget_respected(self, monkeypatch):
        m = self._fake_setup(monkeypatch)
        boot = m._bootstrap_provider(None, None, "", "", "json_fc", 12_345)
        assert boot.max_context_tokens == 12_345

    def test_bootstrap_unknown_format_fails_fast(self, monkeypatch):
        import typer

        m = self._fake_setup(monkeypatch)
        try:
            m._bootstrap_provider(None, None, "", "", "no_such_format", 0)
        except typer.Exit as e:
            assert e.exit_code == 2
        else:  # pragma: no cover
            raise AssertionError("unknown format must exit(2)")

    def test_load_resume_session_fail_fast(self):
        import typer

        import agent_cli.main as m

        try:
            m._load_resume_session("no-such-session-id-000")
        except typer.Exit as e:
            assert e.exit_code == 1
        else:  # pragma: no cover
            raise AssertionError("unknown session must exit(1)")

    def test_compute_token_budget_unified_formula(self):
        from agent_cli.context.manager import compute_token_budget

        assert compute_token_budget(262_144) == (262_144 * 7) // 10
        assert compute_token_budget(1_000) == 4_000  # floor

    def test_run_and_web_have_resume_option(self):
        # run 도 web 과 동일하게 --resume 을 노출 (C4 ③)
        import inspect

        import agent_cli.main as m

        assert "resume" in inspect.signature(m.run).parameters
        assert "resume" in inspect.signature(m.web).parameters


class TestConcurrencyOptionValidation:
    """``agent-cli web`` 의 A1/M4 옵션 게이트 (v7.29.0).

    **조용한 폴백 금지**가 계약의 전부다 — 오타 하나로 "병렬로 돌고 있다"고
    착각한 채 측정하면 실험 결과가 통째로 무효가 된다 (wire-format
    silent-switch 금지와 같은 규율, G1).

    검사는 전부 무거운 배선(세션/provider/uvicorn) **이전**에 실행된다. 그
    사실 자체를 테스트 장치로 쓴다: 게이트 직후의 ``effect_lock.set_scope``
    를 가로채 거기서 기동을 끊으면, 실제 서버를 띄우지 않고도 "게이트를
    통과했는가 + 어떤 스코프로 통과했는가"를 정확히 잰다.
    """

    _SENTINEL = 87  # 게이트 통과 지점에서 기동을 끊는 표식 exit code

    def _invoke(self, tmp_path, monkeypatch, *args):
        """``web`` 을 돌리되 옵션 게이트 직후에 멈춘다.

        반환: ``(result, scopes)`` — ``scopes`` 는 게이트가 실제로 적용하려
        한 락 스코프 목록(통과 못 했으면 빈 리스트).
        """
        import typer
        from typer.testing import CliRunner

        from agent_cli.main import app
        from agent_cli.tools import effect_lock

        scopes: list[str] = []
        real = effect_lock.set_scope

        def spy(scope):
            real(scope)  # 진짜 검증(알 수 없는 값 → ValueError)은 그대로 태운다
            scopes.append(scope)
            raise typer.Exit(self._SENTINEL)  # 여기서 기동 중단

        monkeypatch.setattr(effect_lock, "set_scope", spy)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            app, ["web", "--no-browser", *args], catch_exceptions=False
        )
        return result, scopes

    def test_unknown_contract_exits_2_before_anything_starts(
        self, tmp_path, monkeypatch
    ):
        result, scopes = self._invoke(
            tmp_path, monkeypatch, "--concurrency-contract", "paralell"
        )
        assert result.exit_code == 2
        assert "unknown --concurrency-contract" in result.stdout
        assert scopes == [], "거부됐어야 할 기동이 게이트를 지나갔다"

    def test_zero_cap_under_parallel_exits_2(self, tmp_path, monkeypatch):
        result, scopes = self._invoke(
            tmp_path,
            monkeypatch,
            "--concurrency-contract",
            "parallel",
            "--max-concurrent-turns",
            "0",
        )
        assert result.exit_code == 2
        assert "--max-concurrent-turns must be >= 1" in result.stdout
        assert scopes == []

    def test_cap_is_not_checked_outside_parallel(self, tmp_path, monkeypatch):
        """직렬 계약에서 cap 은 쓰이지 않으므로 값을 트집잡지 않는다."""
        result, scopes = self._invoke(
            tmp_path, monkeypatch, "--max-concurrent-turns", "0"
        )
        assert result.exit_code == self._SENTINEL
        assert scopes == ["off"]

    def test_unknown_lock_scope_exits_2(self, tmp_path, monkeypatch):
        result, _ = self._invoke(tmp_path, monkeypatch, "--lock-scope", "worksapce")
        assert result.exit_code == 2
        assert "unknown lock scope" in result.stdout

    def test_rejected_run_leaves_the_global_scope_untouched(
        self, tmp_path, monkeypatch
    ):
        """실패한 기동이 전역 스코프를 오염시키면 같은 프로세스의 다음 세션이
        의도치 않은 락 설정으로 돈다."""
        from agent_cli.tools import effect_lock

        effect_lock.reset()
        before = effect_lock.get_scope()
        self._invoke(tmp_path, monkeypatch, "--lock-scope", "nonsense")
        assert effect_lock.get_scope() == before
        effect_lock.reset()

    def test_serial_default_keeps_locking_off(self, tmp_path, monkeypatch):
        """기본 경로는 오늘 동작 그대로 — 락 없음."""
        result, scopes = self._invoke(tmp_path, monkeypatch)
        assert result.exit_code == self._SENTINEL
        assert scopes == ["off"]

    def test_parallel_defaults_to_conflict_scope(self, tmp_path, monkeypatch):
        """병렬을 켠 사람은 동시 파일 쓰기 보호를 원한 것이다."""
        _result, scopes = self._invoke(
            tmp_path, monkeypatch, "--concurrency-contract", "parallel"
        )
        assert scopes == ["conflict"]

    def test_reject_contract_does_not_turn_locking_on(self, tmp_path, monkeypatch):
        """거부 계약은 여전히 한 번에 하나만 돈다 — 락이 필요 없다."""
        _result, scopes = self._invoke(
            tmp_path, monkeypatch, "--concurrency-contract", "reject"
        )
        assert scopes == ["off"]

    def test_explicit_off_beats_the_parallel_default(self, tmp_path, monkeypatch):
        """ablation 실험이 계약과 락을 **독립적으로** 조작해야 하므로 —
        '병렬인데 락은 끈' 팔이 P3 대조군이다."""
        _result, scopes = self._invoke(
            tmp_path,
            monkeypatch,
            "--concurrency-contract",
            "parallel",
            "--lock-scope",
            "off",
        )
        assert scopes == ["off"]

    def test_explicit_workspace_beats_the_serial_default(self, tmp_path, monkeypatch):
        """반대 방향 — 직렬인데 워크스페이스 전체 락(포크 v1 재현 팔)."""
        _result, scopes = self._invoke(
            tmp_path, monkeypatch, "--lock-scope", "workspace"
        )
        assert scopes == ["workspace"]

    def test_options_exist_on_the_command(self):
        import inspect

        import agent_cli.main as m

        params = inspect.signature(m.web).parameters
        for name in (
            "concurrency_contract",
            "max_concurrent_turns",
            "per_user_gate",
            "lock_scope",
            "turn_metrics_enabled",
            "spectators",
            "view_token",
        ):
            assert name in params, name
