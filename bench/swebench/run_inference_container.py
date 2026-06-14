#!/usr/bin/env python3
"""SWE-bench inference adapter (B): run agent-cli INSIDE each instance's
SWE-bench container, where the repo + deps + test env are set up, so the
agent can run tests while fixing. More faithful than the host adapter
(run_inference.py), which edits blind.

Per-instance flow:
  build env/instance image → start container → checkout base_commit +
  `pip install -e .` (testbed env) → create a separate `agentcli` conda env
  (py3.11) + install the agent-cli wheel → run agent-cli in /testbed (its
  shell test-runs hit the testbed env; the provider is reached via
  host.docker.internal) → `git diff` → predictions.jsonl + health.

  bench/.venv/bin/python bench/swebench/run_inference_container.py \
      --instances django__django-10914 --out bench/runs/smokeB
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import docker
from datasets import load_dataset
from swebench.harness.constants import BASE_IMAGE_BUILD_DIR, ENV_IMAGE_BUILD_DIR
from swebench.harness.docker_build import (
    build_container,
    build_image,
    setup_logger,
)
from swebench.harness.test_spec.test_spec import make_test_spec

WHEEL = "agent_cli-2.0.0-py3-none-any.whl"
AGENTCLI_PY = "/opt/miniconda3/envs/agentcli/bin"


def agentcli_cache(arch: str) -> Path:
    # arch-specific: the env's python is built for the base image's arch
    return Path(f"bench/.cache/agentcli_env_{arch}.tgz")


def _ensure_image(client, key, dockerfile, platform, scripts, build_dir):
    try:
        client.images.get(key)
        return  # already built
    except docker.errors.ImageNotFound:
        pass
    build_image(key, scripts, dockerfile, platform, client, build_dir)


def build_base_env(spec, client):
    """Build base→env images for this spec's arch (native arm64 or x86).
    Replaces build_env_images, which is hardwired to x86_64."""
    _ensure_image(
        client, spec.base_image_key, spec.base_dockerfile, spec.platform, {},
        BASE_IMAGE_BUILD_DIR / spec.base_image_key.replace(":", "__"),
    )
    _ensure_image(
        client, spec.env_image_key, spec.env_dockerfile, spec.platform,
        {"setup_env.sh": spec.setup_env_script},
        ENV_IMAGE_BUILD_DIR / spec.env_image_key.replace(":", "__"),
    )
PROMPT = """\
Resolve the following GitHub issue in this repository. Read the relevant \
source files, make the necessary code changes to fix the issue, then call \
`complete`. You may run the project's tests to verify your fix. Modify only \
the library/source code — do not edit the test suite. Keep the change minimal.

--- ISSUE ---
{problem_statement}
"""


def ensure_wheel():
    if not (Path("dist") / WHEEL).exists():
        print("building agent-cli wheel…", flush=True)
        subprocess.run(["python3", "-m", "build", "--wheel"], check=True)


def host_provider_config() -> tuple[dict, dict | None]:
    cfg = json.loads(Path(os.path.expanduser("~/.agent-cli/config.json")).read_text())
    # inside the container the host provider is reached via host.docker.internal
    cfg = dict(cfg)
    cfg["base_url"] = "http://host.docker.internal:8000/v1"
    models = None
    mp = Path(os.path.expanduser("~/.agent-cli/models.json"))
    if mp.exists():
        models = json.loads(mp.read_text())
    return cfg, models


def ensure_agentcli_cache(client, base_image_key: str, platform: str, cache: Path):
    """Build the agentcli conda env ONCE and cache it as a tarball. All
    swebench images share /opt/miniconda3, so the env (at the identical path
    /opt/miniconda3/envs/agentcli) is portable across instances of the same
    arch by tar — no relocation needed. Replaces a 3-5min per-instance
    conda+pip install with a ~seconds cp+extract."""
    if cache.exists():
        return
    cache.parent.mkdir(parents=True, exist_ok=True)
    print(f"agentcli env 베이크 (1회, base={base_image_key}, {platform})…", flush=True)
    c = client.containers.run(
        base_image_key, command="sleep infinity", detach=True,
        platform=platform,
    )
    try:
        subprocess.run(["docker", "cp", f"dist/{WHEEL}", f"{c.name}:/tmp/{WHEEL}"], check=True)
        rc = c.exec_run(["bash", "-lc",
            f"conda create -n agentcli python=3.11 -y >/dev/null 2>&1 && "
            f"{AGENTCLI_PY}/pip install -q /tmp/{WHEEL} && "
            f"{AGENTCLI_PY}/agent-cli --version"])
        out = rc.output.decode("utf-8", "replace")
        if rc.exit_code != 0 or "agent-cli" not in out:
            raise RuntimeError(f"agentcli bake failed: {out[-400:]}")
        c.exec_run(["bash", "-lc", "tar czf /tmp/agentcli_env.tgz -C /opt/miniconda3/envs agentcli"])
        subprocess.run(["docker", "cp", f"{c.name}:/tmp/agentcli_env.tgz", str(cache)], check=True)
        print(f"  베이크 완료 → {cache} ({cache.stat().st_size // 1024 // 1024}MB)", flush=True)
    finally:
        c.remove(force=True)


def cp_in(cname: str, content: str, dest: str):
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(content)
        tmp = f.name
    subprocess.run(["docker", "cp", tmp, f"{cname}:{dest}"], check=True)
    os.unlink(tmp)


def run_instance(inst: dict, args, client) -> dict:
    iid = inst["instance_id"]
    print(f"  [{iid}] {inst['repo']} @ {inst['base_commit'][:8]}", flush=True)
    spec = make_test_spec(inst, arch=args.arch)
    build_base_env(spec, client)  # arch-aware base→env (native arm64 or x86)
    logger = setup_logger(iid, Path(args.out) / f"{iid}.build.log")
    container = build_container(spec, client, args.run_id, logger, nocache=False)
    container.start()
    cname = container.name

    def ex(cmd, timeout=None, env=None):
        full = "source /opt/miniconda3/bin/activate testbed && " + cmd
        if timeout:
            full = f"timeout {timeout} bash -lc {sh_quote(full)}"
            r = container.exec_run(["bash", "-lc", full], environment=env or {})
        else:
            r = container.exec_run(["bash", "-lc", full], environment=env or {})
        return r.exit_code, r.output.decode("utf-8", "replace")

    t0 = time.time()
    try:
        # 1. repo at base_commit + (re)install into testbed env
        print("    setup: checkout base_commit + pip install -e .", flush=True)
        ex(
            f"cd /testbed && git checkout -f {inst['base_commit']} && "
            "git clean -fdxq && python -m pip install -e . -q",
            timeout=900,
        )
        ex("echo '.agent-cli/' >> /testbed/.git/info/exclude")

        # 2. separate agentcli env — restore the baked cache (cp+extract,
        #    ~seconds) instead of conda create + pip install (~minutes emulated)
        print("    agentcli env 복원(cache)…", flush=True)
        subprocess.run(["docker", "cp", str(agentcli_cache(args.arch)), f"{cname}:/tmp/agentcli_env.tgz"], check=True)
        ex("mkdir -p /opt/miniconda3/envs && tar xzf /tmp/agentcli_env.tgz -C /opt/miniconda3/envs", timeout=300)
        # provider config (base_url → host.docker.internal) + model caps
        cfg, models = host_provider_config()
        ex("mkdir -p /root/.agent-cli")
        cp_in(cname, json.dumps(cfg), "/root/.agent-cli/config.json")
        if models:
            cp_in(cname, json.dumps(models), "/root/.agent-cli/models.json")

        # 3. run agent-cli (testbed active → shell tests hit testbed py;
        #    agent-cli itself runs on agentcli py via absolute path)
        print("    agent-cli run…", flush=True)
        prompt = PROMPT.format(problem_statement=inst["problem_statement"])
        rc, out = ex(
            f'cd /testbed && {AGENTCLI_PY}/agent-cli run "$TASK" '
            f"--max-turns {args.max_turns}",
            timeout=args.timeout,
            env={"TASK": prompt},
        )
        timed_out = rc == 124  # `timeout` exit code

        # 4. extract patch
        _, patch = ex("cd /testbed && git add -A && git -c core.fileMode=false diff --cached HEAD")

        # 5. pull turns.jsonl for health
        health = pull_health(cname, Path(args.out) / iid)
        elapsed = round(time.time() - t0, 1)
        print(
            f"    → {elapsed}s rc={rc} patch={len(patch)}B "
            f"turns={health.get('turns')} fails={health.get('failures', {})}"
            + (" [TIMEOUT]" if timed_out else ""),
            flush=True,
        )
        return {
            "prediction": {
                "instance_id": iid,
                "model_name_or_path": args.model_name,
                "model_patch": patch,
            },
            "health": {
                "instance_id": iid, "repo": inst["repo"], "elapsed_s": elapsed,
                "returncode": rc, "timed_out": timed_out,
                "patch_bytes": len(patch), "empty_patch": not patch.strip(),
                **health,
            },
        }
    finally:
        try:
            container.stop(timeout=5)
        except Exception:
            pass
        container.remove(force=True)


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def pull_health(cname: str, dest: Path) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    # find newest session turns.jsonl and copy it out
    r = subprocess.run(
        ["docker", "exec", cname, "bash", "-lc",
         "ls -t /testbed/.agent-cli/sessions/*/turns.jsonl 2>/dev/null | head -1"],
        capture_output=True, text=True,
    )
    path = r.stdout.strip()
    if not path:
        return {"turns": 0, "note": "no turns.jsonl"}
    subprocess.run(["docker", "cp", f"{cname}:{path}", str(dest / "turns.jsonl")], check=False)
    tf = dest / "turns.jsonl"
    if not tf.exists():
        return {"turns": 0, "note": "copy failed"}
    fails, stages, total = {}, {}, 0
    for line in tf.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        total += 1
        stages[str(row.get("parse_stage"))] = stages.get(str(row.get("parse_stage")), 0) + 1
        s = row.get("failure_signal")
        if s:
            fails[s] = fails.get(s, 0) + 1
    return {"turns": total, "failures": fails, "parse_stage": stages}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    ap.add_argument("--split", default="test")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--repo", default=None)
    ap.add_argument("--instances", default=None)
    ap.add_argument("--max-turns", type=int, default=25)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--arch", default="x86_64", choices=["x86_64", "arm64"],
                    help="arm64 = native build (no QEMU); x86_64 = emulated on Apple Silicon")
    ap.add_argument("--model-name", default="agent-cli-qwen27b-container")
    ap.add_argument("--run-id", default="smokeB")
    ap.add_argument("--out", default="bench/runs/smokeB")
    args = ap.parse_args()

    ensure_wheel()
    ds = load_dataset(args.dataset, split=args.split)
    rows = list(ds)
    if args.instances:
        want = set(args.instances.split(","))
        rows = [r for r in rows if r["instance_id"] in want]
    elif args.repo:
        rows = [r for r in rows if r["repo"] == args.repo][: args.n]
    else:
        rows = rows[: args.n]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = docker.from_env()
    print(f"[B] 인스턴스 {len(rows)}개 → {out}", flush=True)

    # bake the agentcli env once (shared across all instances of this arch)
    spec0 = make_test_spec(rows[0], arch=args.arch)
    ensure_agentcli_cache(
        client, spec0.base_image_key, spec0.platform, agentcli_cache(args.arch)
    )

    preds, healths = [], []
    for inst in rows:
        try:
            res = run_instance(inst, args, client)
            preds.append(res["prediction"])
            healths.append(res["health"])
        except Exception as e:
            print(f"    ERROR {inst['instance_id']}: {e}", flush=True)
            healths.append({"instance_id": inst["instance_id"], "error": str(e)})

    (out / "predictions.jsonl").write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in preds) + "\n"
    )
    (out / "health.json").write_text(json.dumps(healths, ensure_ascii=False, indent=2))
    print(f"\n[B] 완료: predictions={len(preds)} → {out}/predictions.jsonl", flush=True)


if __name__ == "__main__":
    main()
