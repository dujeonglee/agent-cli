"""Enforced per-turn file capabilities with staged, validated publication.

This is deliberately narrower than a process sandbox: it governs mutations
performed through the cooperative tool boundary.  In capability mode, unknown
workspace effects, shell, and composite agents fail closed because they could
bypass the explicit write set.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from agent_cli import turn_metrics
from agent_cli.tools import _confine
from agent_cli.tools.effect import EffectIntent, EffectKind
from agent_cli.tools.result import ToolResult


def _canonical(path: str | Path, root: Path) -> Path:
    p = Path(path)
    return (p if p.is_absolute() else root / p).resolve()


def _resource_keys(path: Path) -> frozenset[str]:
    keys = {f"path:{path}"}
    try:
        st = path.stat()
    except OSError:
        return frozenset(keys)
    # Existing hard-link aliases collide even when their canonical path names
    # differ.  Symlinks have already collapsed through Path.resolve().
    keys.add(f"inode:{st.st_dev}:{st.st_ino}")
    return frozenset(keys)


def _version(path: Path) -> tuple[int, int, int, str] | None:
    try:
        st = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return st.st_dev, st.st_ino, st.st_size, digest
    except FileNotFoundError:
        return None


class _ReservationRegistry:
    """Process-local reservation gate for canonical/inode write identities."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._held: dict[str, frozenset[str]] = {}

    def acquire(self, owner: str, keys: frozenset[str]) -> tuple[bool, float]:
        with self._cv:
            conflicted = any(keys & other for other in self._held.values())
            started = time.monotonic()
            while any(keys & other for other in self._held.values()):
                self._cv.wait()
            self._held[owner] = keys
            return conflicted, (time.monotonic() - started) * 1000.0

    def release(self, owner: str) -> None:
        with self._cv:
            self._held.pop(owner, None)
            self._cv.notify_all()


_RESERVATIONS = _ReservationRegistry()


Validator = Callable[[Mapping[Path, Path]], bool | tuple[bool, str]]


def _canonical_text(text: str) -> str:
    """Canonical text-file oracle: ignore one conventional final newline.

    This matches the paper's existing exact line-content scorer while keeping
    every other byte significant (including extra blank lines or spaces).
    """
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


@dataclass(frozen=True)
class TurnIsolationPolicy:
    """Authorization and validation supplied by the requesting task."""

    turn_id: str
    allowed_paths: Sequence[str | Path]
    expected_contents: Mapping[str | Path, str] | None = None
    validator: Validator | None = None
    workspace_root: Path | None = None


@dataclass
class TurnIsolation:
    """One turn's capability, staging area, reservation, and audit trail."""

    policy: TurnIsolationPolicy
    events: list[dict] = field(default_factory=list, init=False)
    _root: Path = field(init=False)
    _allowed: dict[Path, frozenset[str]] = field(init=False)
    _baseline: dict[Path, tuple[int, int, int, str] | None] = field(init=False)
    _staged: dict[Path, Path] = field(default_factory=dict, init=False)
    _stage_dir: Path | None = field(default=None, init=False)
    _entered: bool = field(default=False, init=False)
    _used: bool = field(default=False, init=False)
    _finished: bool = field(default=False, init=False)
    _reservation_owner: str = field(init=False)

    def __post_init__(self) -> None:
        self._root = (self.policy.workspace_root or _confine.workspace_root()).resolve()
        allowed: dict[Path, frozenset[str]] = {}
        for raw in self.policy.allowed_paths:
            p = _canonical(raw, self._root)
            if not (p == self._root or self._root in p.parents):
                raise ValueError(f"write capability escapes workspace: {raw}")
            if not p.parent.is_dir():
                raise ValueError(
                    f"write capability parent does not exist: {p.parent}; "
                    "directory capabilities are not implemented"
                )
            allowed[p] = _resource_keys(p)
        if not allowed:
            raise ValueError("turn isolation requires a non-empty write capability")
        self._allowed = allowed
        self._baseline = {}
        self._reservation_owner = f"{self.policy.turn_id}:{id(self)}"

    def _emit(self, phase: str, **fields) -> None:
        row = {"phase": phase, "turn_id": self.policy.turn_id, **fields}
        self.events.append(row)
        turn_metrics.emit("isolation", **row)

    @property
    def allowed_paths(self) -> tuple[str, ...]:
        return tuple(str(p) for p in sorted(self._allowed, key=str))

    def __enter__(self) -> Self:
        if self._used:
            raise RuntimeError("a turn isolation transaction cannot be reused")
        self._used = True
        keys = frozenset(k for values in self._allowed.values() for k in values)
        conflicted, wait_ms = _RESERVATIONS.acquire(self._reservation_owner, keys)
        try:
            # The version boundary starts only after this turn owns every resource.
            # A conflicting predecessor may have published while we waited; that
            # committed state is this turn's legitimate base, not a false conflict.
            self._baseline = {p: _version(p) for p in self._allowed}
            self._stage_dir = Path(tempfile.mkdtemp(prefix="agent-cli-turn-stage-"))
            self._entered = True
        except BaseException:
            _RESERVATIONS.release(self._reservation_owner)
            raise
        if conflicted:
            self._emit("reservation_wait", wait_ms=wait_ms)
        self._emit(
            "capability_granted",
            paths=[str(p) for p in sorted(self._allowed, key=str)],
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._stage_dir is not None:
                shutil.rmtree(self._stage_dir, ignore_errors=True)
        finally:
            if self._entered:
                _RESERVATIONS.release(self._reservation_owner)
                self._entered = False

    def _approved(self, raw_path: str) -> Path | None:
        try:
            candidate = _canonical(raw_path, self._root)
        except (OSError, RuntimeError, ValueError):
            return None
        return candidate if candidate in self._allowed else None

    def authorize_tool(
        self, tool_name: str, args: dict, intent: EffectIntent
    ) -> str | None:
        """Return a denial message, or ``None`` when the call may proceed."""
        if tool_name in {"agent", "run_skill"}:
            return self._blocked(tool_name, "composite tool can bypass the capability")
        if intent.kind is EffectKind.FILE_WRITE:
            raw = intent.path or str(args.get("path", ""))
            if not raw or self._approved(raw) is None:
                return self._blocked(
                    tool_name, f"path is outside approved write set: {raw!r}"
                )
            return None
        if intent.kind is EffectKind.FILE_READ:
            return None
        if intent.kind in {
            EffectKind.SHELL,
            EffectKind.PACKAGE,
            EffectKind.FILE_DELETE,
            EffectKind.UNKNOWN_WORKSPACE_EFFECT,
        }:
            return self._blocked(
                tool_name, f"unscoped workspace effect: {intent.kind.value}"
            )
        return None

    def _blocked(self, tool_name: str, reason: str) -> str:
        self._emit("effect_blocked", tool=tool_name, reason=reason)
        return f"Turn isolation blocked '{tool_name}': {reason}"

    def stage_for_write(self, raw_path: str) -> Path:
        logical = self._approved(raw_path)
        if logical is None:
            raise PermissionError(f"path is outside approved write set: {raw_path!r}")
        existing = self._staged.get(logical)
        if existing is not None:
            return existing
        assert self._stage_dir is not None, "turn isolation was not entered"
        token = hashlib.sha256(str(logical).encode()).hexdigest()
        staged = self._stage_dir / token / logical.name
        staged.parent.mkdir(parents=True, exist_ok=True)
        if logical.exists():
            shutil.copy2(logical, staged)
        self._staged[logical] = staged
        return staged

    def path_for_read(self, raw_path: str) -> Path | None:
        logical = self._approved(raw_path)
        return self._staged.get(logical) if logical is not None else None

    def finish(self, result: ToolResult) -> ToolResult:
        """Validate and publish, or leave the shared workspace unchanged."""
        if self._finished:
            return ToolResult(
                False, error="Turn isolation transaction already finished"
            )
        self._finished = True
        if not result.success:
            self._emit("publication_aborted", reason="turn_failed")
            return result
        ok, detail = self._validate()
        if not ok:
            self._emit("validation_failed", reason=detail)
            return ToolResult(
                False, error=f"Turn isolation validation failed: {detail}"
            )
        self._emit("validation_passed", files=len(self._staged))
        conflict = next(
            (p for p in self._staged if _version(p) != self._baseline[p]), None
        )
        if conflict is not None:
            self._emit("commit_conflict", path=str(conflict))
            return ToolResult(
                False,
                error=f"Turn isolation commit conflict: '{conflict}' changed after dispatch",
            )
        try:
            self._publish()
        except OSError as exc:
            self._emit("publication_failed", reason=str(exc))
            return ToolResult(False, error=f"Turn isolation publication failed: {exc}")
        self._emit(
            "write_set_published",
            files=len(self._staged),
            paths=[str(p) for p in sorted(self._staged, key=str)],
        )
        return result

    def _validate(self) -> tuple[bool, str]:
        expected = self.policy.expected_contents
        if expected is not None:
            expected_map = {
                _canonical(path, self._root): content
                for path, content in expected.items()
            }
            if set(expected_map) != set(self._allowed):
                return False, "exact oracle must cover the complete approved write set"
            for logical, wanted in expected_map.items():
                staged = self._staged.get(logical)
                if staged is None or not staged.exists():
                    return False, f"approved output was not staged: {logical}"
                if _canonical_text(
                    staged.read_text(encoding="utf-8")
                ) != _canonical_text(wanted):
                    return False, f"exact-content mismatch: {logical}"
        if self.policy.validator is not None:
            verdict = self.policy.validator(dict(self._staged))
            if isinstance(verdict, tuple):
                if not verdict[0]:
                    return False, verdict[1]
            elif not verdict:
                return False, "task validator rejected staged write set"
        if expected is None and self.policy.validator is None:
            return False, "no task-supplied oracle; automatic publication is disabled"
        return True, ""

    def _publish(self) -> None:
        """Validate-all-first, then atomically replace each target file.

        Each file publication is atomic.  The reservation prevents another
        cooperating turn from observing a conflicting in-process commit; a
        process crash between files remains outside the stated guarantee.
        """
        prepared: list[tuple[Path, Path]] = []
        for logical, staged in self._staged.items():
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{logical.name}.turn-", dir=str(logical.parent)
            )
            os.close(fd)
            tmp = Path(tmp_name)
            shutil.copy2(staged, tmp)
            prepared.append((logical, tmp))
        try:
            for logical, tmp in prepared:
                os.replace(tmp, logical)
        finally:
            for _, tmp in prepared:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
