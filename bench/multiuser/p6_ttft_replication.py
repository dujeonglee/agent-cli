#!/usr/bin/env python3
"""Prospective, fully manifested live-model TTFT replication.

Unlike the historical P6 spot check, this collector refuses to start unless it
can archive the serving-engine version, model configuration, and server
hardware.  Serial and parallel runs are paired in blocks and the first arm is
balanced with a fixed randomization seed.  Every run, including failures, is
written incrementally so an interrupted collection remains auditable.

Usage::

    AGENT_CLI_BASE_URL=... AGENT_CLI_API_KEY=... AGENT_CLI_MODEL=... \
      .venv/bin/python bench/multiuser/p6_ttft_replication.py --reps 20
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from driver import AgentServer, ttft_ms, turn_chain

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = Path(__file__).parent / "out" / "p6-ttft-replication.json"
LONG_TASK = (
    "Count from 1 to 120, one number per line, in plain text. "
    "Do not use any tools. When finished call complete with result 'counted'."
)
SHORT_TASK = "Reply with just the word pong. Then call complete with result 'pong'."


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def real_llm_from_env() -> dict[str, str]:
    try:
        return {
            "base_url": os.environ["AGENT_CLI_BASE_URL"],
            "api_key": os.environ["AGENT_CLI_API_KEY"],
            "model": os.environ["AGENT_CLI_MODEL"],
        }
    except KeyError as exc:
        raise SystemExit(
            f"missing env {exc} — set AGENT_CLI_BASE_URL/API_KEY/MODEL"
        ) from exc


def _origin(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("AGENT_CLI_BASE_URL must be an HTTP(S) URL")
    return f"{parsed.scheme}://{parsed.netloc}"


def _json_get(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    api_key: str | None = None,
) -> Any:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=20) as response:
        return json.load(response)


def _admin_opener(origin: str, api_key: str) -> urllib.request.OpenerDirector:
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    query = urllib.parse.urlencode(
        {"key": api_key, "redirect": "/admin/dashboard"}
    )
    # oMLX's documented menubar-app flow exchanges the API key for a temporary
    # admin session cookie.  Neither value is persisted in the artifact.
    opener.open(f"{origin}/admin/auto-login?{query}", timeout=20).read(100)
    if not list(cookies):
        raise RuntimeError("server did not establish an admin manifest session")
    return opener


def endpoint_status(llm: dict[str, str]) -> dict[str, Any]:
    opener = urllib.request.build_opener()
    status = _json_get(
        opener,
        f"{_origin(llm['base_url'])}/api/status",
        api_key=llm["api_key"],
    )
    keep = (
        "uptime_seconds",
        "models_loaded",
        "total_requests",
        "active_requests",
        "waiting_requests",
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_cached_tokens",
        "cache_efficiency",
        "avg_prefill_tps",
        "avg_generation_tps",
        "model_memory_used",
    )
    return {key: status.get(key) for key in keep}


def collect_manifest(llm: dict[str, str]) -> dict[str, Any]:
    origin = _origin(llm["base_url"])
    public = urllib.request.build_opener()
    openapi = _json_get(
        public, f"{origin}/openapi.json", api_key=llm["api_key"]
    )
    service = _json_get(
        public, f"{origin}/api/status", api_key=llm["api_key"]
    )
    models = _json_get(
        public, f"{origin}/v1/models/status", api_key=llm["api_key"]
    )
    selected = next(
        (item for item in models.get("models", []) if item.get("id") == llm["model"]),
        None,
    )
    if selected is None:
        raise RuntimeError(f"selected model is absent from /v1/models/status: {llm['model']}")

    admin = _admin_opener(origin, llm["api_key"])
    device = _json_get(admin, f"{origin}/admin/api/device-info")
    admin_models = _json_get(admin, f"{origin}/admin/api/models")
    admin_model = next(
        (
            item
            for item in admin_models.get("models", [])
            if item.get("id") == llm["model"]
        ),
        None,
    )
    if admin_model is None:
        raise RuntimeError(f"selected model is absent from /admin/api/models: {llm['model']}")
    global_settings = _json_get(admin, f"{origin}/admin/api/global-settings")
    hardware = {
        key: device.get(key)
        for key in ("chip_name", "chip_variant", "memory_gb", "gpu_cores")
    }
    missing_hardware = [key for key, value in hardware.items() if value in (None, "")]
    if missing_hardware:
        raise RuntimeError(f"server hardware manifest is incomplete: {missing_hardware}")

    engine = {
        "name": openapi.get("info", {}).get("title"),
        "version": openapi.get("info", {}).get("version"),
        "api_status_version": service.get("version"),
    }
    if not engine["name"] or not engine["version"]:
        raise RuntimeError("serving-engine name/version is unavailable")

    model = {
        key: selected.get(key)
        for key in (
            "id",
            "actual_size",
            "engine_type",
            "model_type",
            "config_model_type",
            "model_context_length",
            "max_context_window",
            "max_tokens",
            "thinking_default",
            "preserve_thinking_default",
            "source_type",
            "source_repo_id",
        )
    }
    model["display_name"] = admin_model.get("display_name")
    model_settings = admin_model.get("settings", {})
    global_sampling = global_settings.get("sampling", {})
    effective_temperature = model_settings.get("temperature")
    if effective_temperature is None:
        effective_temperature = global_sampling.get("temperature")
    effective_top_p = model_settings.get("top_p")
    if effective_top_p is None:
        effective_top_p = global_sampling.get("top_p")
    return {
        "capturedAtUtc": utc_now(),
        "endpoint": {
            "transport": "OpenAI-compatible HTTP API",
            "baseUrl": "redacted; host identity is not required to replay the protocol",
            "engine": engine,
        },
        "serverHardware": hardware,
        "model": model,
        "requestConfiguration": {
            "providerRoute": "/v1/chat/completions",
            "stream": True,
            "streamOptions": {"include_usage": True},
            "temperatureRequestField": "omitted",
            "effectiveTemperature": effective_temperature,
            "topPRequestField": "omitted",
            "effectiveTopP": effective_top_p,
            "seedRequestField": "omitted; nondeterministic sampling",
            "agentCliDetectedContextWindow": int(model["model_context_length"]),
            "agentCliRequestedMaxTokens": int(model["model_context_length"]) // 4,
            "serverConfiguredMaxTokens": model["max_tokens"],
            "enableThinking": model_settings.get("enable_thinking"),
            "forceSampling": model_settings.get("force_sampling"),
            "responseFormat": "agent-cli json_fc structured-text wire format",
            "toolCalling": "text wire format parsed by agent-cli; no native tool_choice",
        },
        "servingConfiguration": {
            "server": {
                key: global_settings.get("server", {}).get(key)
                for key in ("burst_decode_mode", "preserve_mid_system_cache")
            },
            "scheduler": {
                key: global_settings.get("scheduler", {}).get(key)
                for key in (
                    "max_concurrent_requests",
                    "chunked_prefill",
                    "prefill_priority",
                )
            },
            "cache": {
                key: global_settings.get("cache", {}).get(key)
                for key in ("enabled", "hot_cache_only", "initial_cache_blocks")
            },
        },
        "serverStateAtManifest": endpoint_status(llm),
    }


def source_manifest() -> dict[str, Any]:
    paths = sorted((ROOT / "agent_cli").rglob("*.py")) + [
        Path(__file__).resolve(),
        Path(__file__).with_name("driver.py").resolve(),
    ]
    digest = hashlib.sha256()
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()

    try:
        commit = git("rev-parse", "HEAD")
        status = subprocess.check_output(
            ["git", "status", "--short"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        dirty_paths = [line[3:] for line in status.splitlines() if len(line) >= 4]
    except (OSError, subprocess.CalledProcessError):
        commit = "unavailable"
        dirty_paths = []
    return {
        "gitCommit": commit,
        "worktreeDirty": bool(dirty_paths),
        "dirtyPaths": dirty_paths,
        "sourceDigestSha256": digest.hexdigest(),
        "sourceFileCount": len(paths),
        "digestScope": "agent_cli/**/*.py + p6_ttft_replication.py + driver.py",
        "python": sys.version.split()[0],
        "clientPlatform": platform.platform(),
        "clientLogicalCpuCount": os.cpu_count(),
    }


def balanced_orders(reps: int, seed: int) -> list[list[str]]:
    orders = [
        ["serial", "parallel"] if block % 2 == 0 else ["parallel", "serial"]
        for block in range(reps)
    ]
    random.Random(seed).shuffle(orders)
    return orders


def percentile_bootstrap_median(
    values: list[float], *, seed: int, resamples: int = 10_000
) -> list[float]:
    rng = random.Random(seed)
    n = len(values)
    samples = sorted(
        statistics.median(rng.choices(values, k=n)) for _ in range(resamples)
    )
    return [samples[int(0.025 * resamples)], samples[int(0.975 * resamples) - 1]]


def summarize(runs: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    arms: dict[str, Any] = {}
    for offset, arm in enumerate(("serial", "parallel")):
        arm_runs = [run for run in runs if run["arm"] == arm]
        values = [float(run["bTtftMs"]) for run in arm_runs if run["valid"]]
        arms[arm] = {
            "planned": len(arm_runs),
            "valid": len(values),
            "failed": len(arm_runs) - len(values),
            "medianMs": round(statistics.median(values), 1) if values else None,
            "rangeMs": [round(min(values), 1), round(max(values), 1)]
            if values
            else None,
            "medianBootstrapCi95Ms": [round(x, 1) for x in percentile_bootstrap_median(
                values, seed=seed + offset
            )]
            if values
            else None,
        }
    by_block: dict[int, dict[str, float]] = {}
    for run in runs:
        if run["valid"]:
            by_block.setdefault(int(run["block"]), {})[run["arm"]] = float(
                run["bTtftMs"]
            )
    ratios = [
        row["serial"] / row["parallel"]
        for row in by_block.values()
        if set(row) == {"serial", "parallel"}
    ]
    return {
        "arms": arms,
        "completePairedBlocks": len(ratios),
        "pairedSpeedupMedian": round(statistics.median(ratios), 3)
        if ratios
        else None,
        "bootstrap": {
            "method": "percentile bootstrap of the median",
            "resamples": 10_000,
            "seedSerial": seed,
            "seedParallel": seed + 1,
        },
    }


def write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def seed_model_capabilities(ws: Path, manifest: dict[str, Any]) -> None:
    """Pin capabilities so a fresh HOME does not send a warm-up probe.

    AgentServer intentionally isolates HOME per run.  Without this seed the
    first startup asks the endpoint to detect model capabilities, adding one
    unmeasured request immediately before every TTFT trial.  The values below
    come from the same endpoint manifest archived with the experiment.
    """
    model = manifest["model"]
    context_window = int(model["model_context_length"])
    config = {
        "models": {
            model["id"]: {
                "context_window": context_window,
                "max_output_tokens": context_window // 4,
                "supports_thinking": False,
                "thinking_budget": 0,
                "thinking_format": "",
                "_experiment_manifest_seeded": True,
            }
        },
        "provider_defaults": {},
    }
    target = ws / ".agent-cli" / "models.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def collect_run(
    llm: dict[str, str],
    *,
    server_manifest: dict[str, Any],
    block: int,
    arm: str,
    position: int,
    timeout: float,
    delay: float,
) -> dict[str, Any]:
    started = utc_now()
    before_state = endpoint_status(llm)
    ws = Path(tempfile.mkdtemp(prefix=f"p6ttft-{block:02d}-{arm}-"))
    seed_model_capabilities(ws, server_manifest)
    server: AgentServer | None = None
    started_mono = time.monotonic()
    run: dict[str, Any] = {
        "block": block,
        "position": position,
        "arm": arm,
        "startedAtUtc": started,
        "valid": False,
        "bTtftMs": None,
        "failure": None,
        "endpointBefore": before_state,
    }
    try:
        server = AgentServer(ws, None, contract=arm, max_turns=2, real_llm=llm)
        before_events = len(server.events())
        if server.chat(LONG_TASK, f"A-{block}-{arm}") != 200:
            raise RuntimeError("long request was not accepted")
        time.sleep(delay)
        if server.chat(SHORT_TASK, f"B-{block}-{arm}") != 200:
            raise RuntimeError("short request was not accepted")
        events = server.wait_completes_since(before_events, 2, timeout=timeout)
        value = ttft_ms(turn_chain(events, f"B-{block}-{arm}"))
        if value is None:
            raise RuntimeError("no attributable first token for the short request")
        run["bTtftMs"] = round(float(value), 1)
        run["valid"] = True
        run["topLevelLlmCalls"] = sum(
            event.get("event") == "llm_call" and "depth" not in event
            for event in events[before_events:]
        )
    except Exception as exc:  # every failed run must remain in the artifact
        run["failure"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if server is not None:
            server.stop()
        shutil.rmtree(ws, ignore_errors=True)
        run["completedAtUtc"] = utc_now()
        run["wallMs"] = round((time.monotonic() - started_mono) * 1000, 1)
        run["endpointAfter"] = endpoint_status(llm)
        before_requests = before_state.get("total_requests")
        after_requests = run["endpointAfter"].get("total_requests")
        run["endpointRequestDelta"] = (
            after_requests - before_requests
            if isinstance(before_requests, int) and isinstance(after_requests, int)
            else None
        )
        expected_requests = run.get("topLevelLlmCalls")
        if (
            run["valid"]
            and isinstance(expected_requests, int)
            and run["endpointRequestDelta"] != expected_requests
        ):
            run["valid"] = False
            run["failure"] = {
                "type": "EndpointRequestInterference",
                "message": "endpoint request delta did not match local top-level LLM calls",
                "expected": expected_requests,
                "observed": run["endpointRequestDelta"],
            }
    return run


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=20, help="paired blocks/arm")
    parser.add_argument("--seed", type=int, default=20260813)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    if args.reps < 2:
        parser.error("--reps must be at least 2")

    llm = real_llm_from_env()
    orders = balanced_orders(args.reps, args.seed)
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "status": "running",
        "startedAtUtc": utc_now(),
        "protocol": {
            "repetitionsPerArm": args.reps,
            "pairedBlocks": args.reps,
            "shortRequestDelaySeconds": args.delay,
            "runTimeoutSeconds": args.timeout,
            "orderRandomizationSeed": args.seed,
            "blockOrders": orders,
            "serialFirstBlocks": sum(order[0] == "serial" for order in orders),
            "parallelFirstBlocks": sum(order[0] == "parallel" for order in orders),
            "sessionPolicy": "fresh agent process, session, HOME, and workspace per run",
            "startupProbePolicy": "model capabilities seeded from the archived endpoint manifest; no pre-trial model request",
            "longTask": LONG_TASK,
            "shortTask": SHORT_TASK,
            "analysisUnit": "one run; serial/parallel runs paired by block",
        },
        "environment": {
            "server": collect_manifest(llm),
            "clientAndSource": source_manifest(),
        },
        "runs": [],
        "summary": None,
    }
    write_result(args.out, result)

    for block, order in enumerate(orders, start=1):
        for position, arm in enumerate(order, start=1):
            run = collect_run(
                llm,
                server_manifest=result["environment"]["server"],
                block=block,
                arm=arm,
                position=position,
                timeout=args.timeout,
                delay=args.delay,
            )
            result["runs"].append(run)
            result["summary"] = summarize(result["runs"], args.seed)
            write_result(args.out, result)
            print(
                json.dumps(
                    {
                        "block": block,
                        "position": position,
                        "arm": arm,
                        "valid": run["valid"],
                        "bTtftMs": run["bTtftMs"],
                        "wallMs": run["wallMs"],
                        "failure": run["failure"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    result["status"] = "complete"
    result["completedAtUtc"] = utc_now()
    result["environment"]["serverStateAtEnd"] = endpoint_status(llm)
    result["summary"] = summarize(result["runs"], args.seed)
    write_result(args.out, result)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
