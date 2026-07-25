---
name: orchestrate
description: Bootstrap an autonomous agent team for a multi-step task — plan it, spawn an orchestrator agent plus the workers it needs (code-writer, code-reviewer, code-analyst, unittest-writer, log-analyst), hand the roster to the orchestrator, and return. The orchestrator then drives the whole job over peer messages without waking main; main gets the final report (or escalations) automatically. Use when a task needs more than one specialist or more than a few steps.
argument-hint: "<task or feature description>"
allowed-tools: [read_file, write_file, shell, agent]
---

You are bootstrapping an autonomous team for the task in $ARGUMENTS. Your job is
ONLY setup: plan → spawn → hand off → report back. You do NOT assign work to any
worker, and you do NOT coordinate — the orchestrator agent does that, over peer
messages, after you finish.

## Why this shape

Worker replies are routed to whoever requested the work. If YOU (main's skill)
assigned tasks, every completion would wake main. By spawning the workers idle and
letting the ORCHESTRATOR assign everything, all coordination traffic (assign →
reply → review → fix) stays between the agents; main is woken only by the
orchestrator's final report or a genuine escalation.

## Steps

1. **Plan.** For a non-trivial task, run the `plan` skill (or sketch the steps
   inline) so there is an ordered task list with dependencies and file scopes —
   ideally written to `plan/<name>.md` so the orchestrator can read it.

2. **Spawn the workers, idle.** Spawn each specialist the plan calls for — with a
   distinct `name`, and NO `task` field. Do not send them any request; they must
   sit idle until the orchestrator assigns work. Typical team: one or more
   `code-writer`s (disjoint file scopes), a `code-reviewer`, a `unittest-writer`;
   add a `code-analyst` when unfamiliar code must be mapped first, a `log-analyst`
   when failures will need diagnosing. Note each returned agent KEY.

3. **Spawn the orchestrator LAST, with the full briefing as its initial task.**
   `spawn` the `orchestrator` profile; its `task` must contain everything it needs
   to run autonomously:
   - the goal ($ARGUMENTS, restated concretely),
   - the plan file path (or the inline step list),
   - the worker roster: each worker's KEY, profile, name, and assigned scope,
   - the standing instruction: "Coordinate these workers over `message` to
     complete the goal. Work autonomously; do not contact main except for your
     final report or a blocking problem. If you need another worker spawned,
     `message` main to ask."

4. **Report and finish.** `complete` immediately with a handoff brief: the plan
   location, the spawned roster (keys + scopes), and a note that the orchestrator
   is now driving the job asynchronously — its reports will be delivered to main
   automatically, and the user can watch or intervene any time via the agents
   panel (`@agents`, or messaging a key directly).

## Rules

- **Never send a worker a task or request — not one.** Spawn them idle. Any work
  you hand out directly will route its reply to main and defeat the design.
- **Do not wait, poll, or ask for status.** After spawning the orchestrator,
  finish. Replies are delivered to main automatically when they come.
- One writer per file scope; state each scope in the roster you hand over.
- Right-size the team: a 2-step task may need just one writer and a reviewer — do
  not spawn agents the plan gives no work to.
