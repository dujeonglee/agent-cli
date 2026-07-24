---
name: orchestrate
description: Drive a multi-step task by planning it and delegating to the built-in worker agents (code-writer, code-reviewer, code-analyst, unittest-writer, log-analyst). You stay the orchestrator — you plan, spawn persistent workers, fan out independent work, loop writer↔reviewer until clean, and verify. Use when a task needs more than one specialist or more than a few steps.
argument-hint: "<task or feature description>"
allowed-tools: [read_file, write_file, shell, agent]
---

You are the orchestrator for the task in $ARGUMENTS. You do NOT do the specialist
work yourself — you decompose it, delegate to the right worker agent, and integrate
the results. Only YOU (the main agent) can spawn and manage persistent agents;
workers cannot spawn further agents, so keep the coordination here.

## The workers you delegate to

| Need | Worker | Notes |
|------|--------|-------|
| Understand how existing code works (call paths, lifetimes) | `code-analyst` | read-only |
| Write / edit implementation | `code-writer` | one file-scope each; spawn several for disjoint files |
| Review a change for defects | `code-reviewer` | read-only; feeds fixes back to code-writer |
| Write tests that catch bugs | `unittest-writer` | mutation-checked |
| Diagnose a failure / crash / log | `log-analyst` | read-only; root cause |

## How to run it

1. **Understand and plan.** If the task is non-trivial, run the `plan` skill (or
   sketch the steps inline) to get an ordered task list with dependencies and file
   scopes. Decide which steps are independent (can run in parallel) and which must
   be sequential.

2. **Analyze first when the ground is unfamiliar.** If the change touches code you
   do not understand yet, `agent run` a `code-analyst` task to map it before writing
   — cheaper than a writer rediscovering it.

3. **Spawn persistent workers for iterative work.** For work that evolves over
   several rounds (implement → review → fix), `spawn` the workers so they KEEP their
   context between requests — a `run` is one-shot and forgets. Give each a distinct
   `name`. Assign each code-writer a disjoint file scope so parallel writers do not
   collide. Use one-shot `agent run` only for a self-contained task whose result you
   need once (a quick analysis, a single independent file).

4. **Coordinate the loop.** Drive implement → review → fix:
   - `request` the code-writer to implement its scope.
   - `request` the code-reviewer to review the change; it reports defects with
     severity and `file:line`.
   - Feed confirmed defects back to the same code-writer (it still has its context)
     and re-review until the reviewer is clean. Replies are delivered to you
     automatically — do not poll.

5. **Verify.** `request` the `unittest-writer` to add/extend tests for the new
   behavior and run them; if a run fails, hand the failure output to a `log-analyst`
   (or the reviewer) to find the root cause before looping back to the writer. Run
   the project's own checks (build / lint / full test suite) yourself and report the
   result honestly, including failures.

6. **Integrate and report.** Collect the `Files touched:` lists, confirm the pieces
   fit, and report what was done, what was verified, and anything left open. Kill
   persistent workers you no longer need.

## Principles

- **Keep worker context alive.** Prefer `spawn` + `request` over repeated `run` for
  anything iterative — re-deriving context every turn is the main failure mode of
  naive fan-out.
- **One scope per writer.** Parallel code-writers must own disjoint files; a shared
  file is a sequential dependency, not a parallel one.
- **You own integration and verification.** Workers report honestly within their
  scope; making the pieces fit and running the real checks is your job.
- **Right-size it.** A two-step task does not need the full loop — delegate the one
  specialist step and move on. Reserve the full orchestration for genuinely
  multi-part work.

## Complete result format

When done, `complete` with: what was built, which workers did what, what you
verified (tests/build/lint results — real, not assumed), and any open items.
