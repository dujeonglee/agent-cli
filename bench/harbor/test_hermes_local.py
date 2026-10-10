"""`hermes_local.HermesLocal` — harbor 0.24.0 내장 Hermes 어댑터의 두 자리 수정.

harbor 는 벤치 전용 의존성이라(uvx 격리) 없으면 건너뛴다.
"""

from __future__ import annotations

import pytest

harbor = pytest.importorskip("harbor.agents.installed.hermes")


class TestHermesLocal:
    def test_version_is_a_flag_not_a_subcommand(self):
        from hermes_local import HermesLocal

        cmd = HermesLocal.get_version_command(HermesLocal.__new__(HermesLocal))
        assert cmd.endswith("hermes --version")
        assert "hermes version" not in cmd

    def test_install_primes_lazy_dependencies_before_printing_the_version(self):
        """``--version`` 은 지연 의존성 준비를 건너뛰므로(venv_sync) 설치 끝에
        플래그 없는 서브커맨드가 먼저 와야 한다."""
        import inspect

        from hermes_local import HermesLocal

        src = inspect.getsource(HermesLocal.install)
        assert '"hermes sessions && hermes --version"' in src
        assert "--skip-setup" in src
