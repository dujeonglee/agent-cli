# Multiplayer Coding Agents: Concurrency Contracts for Synchronous Multi-User Sharing of a Single LLM Agent Session

> Full paper draft v0.2 — 2026-08-05. Based on `07-new-paper-draft.md`.
> Status notes for authors appear as `[NOTE: …]` and `[TODO: …]` and must be removed before submission.
> Numbers marked ✔ are backed by committed raw data in `Aidit-Code/backend/bench/out/`.
> Naming: the system is presented as **Coagora**; the repository is currently named `Aidit-Code` (paths in author notes keep the repo name; rename repo or use anonymized artifact link before submission).

---

## Abstract

AI coding agents write files, run shells, and open pull requests — yet every deployed agent assumes a single user. Teams that want to use an agent together are funneled through messenger integrations (Claude Tag, OpenAI Codex, GitHub Copilot, Devin, and Cursor in Slack), which share a common architecture: an @-mention triggers the agent, the thread history is absorbed as context, work executes in a detached cloud sandbox, and a pull-request link is posted back. This asynchronous-delegation design offers no point of intervention while the agent runs and no coordination protocol when multiple users issue instructions concurrently. The obvious alternative — letting several users share one live agent session — runs into what practitioners treat as a forced choice: either serialize inference (so my question waits behind your fifteen-second build) or fork the session per user (losing the shared context that made collaboration attractive).

We show there is a third option. We (i) formalize a four-axis design space for multi-user coding-agent interaction — *state locus*, *concurrency contract*, *attribution unit*, and *intervention point* — and locate nine existing systems in it; (ii) propose the concurrency contract **parallel inference, serialized side effects**, which occupies the previously empty coordinate: concurrent user inputs are inferred as independent, simultaneously in-flight turns over a single shared conversation context, while all side effects — file writes, tool executions, and context commits — pass through a conflict-scoped hierarchical lock; and (iii) validate the contract in Coagora, an open-source collaborative coding platform in which every post is bound 1:1 to an isolated sandbox running a shared agent session that any participant can join, steer, and observe in real time.

In 180 measured runs, a second user's time-to-first-token was fully independent of the first user's task length (regression slope of TTFT against task length: 0.000, versus 1.000 for FIFO serialization and 1.010 for a reject-and-retry gate — the strategy used by deployed multi-user bot frameworks — which is *worse than serial*). Removing the serial executor caused file-integrity violations in 74% of concurrent same-file writes; the contract reduces them to 0% without merges, rollbacks, or optimistic concurrency control. We close by analyzing two problems that sharing a session *creates* — asymmetric grounding and multi-party control arbitration — and argue they define the research agenda for multiplayer human–agent programming.

---

## 1 Introduction

Coding agents have crossed a threshold. Systems built on large language models no longer merely suggest completions; they read repositories, edit files, execute shells, run tests, and open pull requests with limited supervision [11, 12]. Controlled studies find that agents complete tasks their users could not have finished alone [12], and large-scale telemetry shows agents authoring the entirety of the code in 41% of real sessions [13]. Yet across this entire literature — and across every deployed product we survey in §2 — one assumption holds so uniformly that it is rarely stated: **one human, one agent.** The interaction model of the modern coding agent is single-player.

Software, however, is not built by individuals. The mismatch is now visible in industry. GitHub Next's Ace prototype opens with the declaration that "as of early 2026, every coding agent is designed as a single-player experience — but building software is not a single-player game," and attributes wasted work, coordination debt, and misaligned outputs to exactly this gap [24]. Users of Claude Code have filed requests for Google-Docs-style shared sessions [25]. The demand precedes the research: we know of no published study of multiple humans synchronously sharing one autonomous coding-agent session (§2.3), and no system paper describing how such sharing should be coordinated.

**The messenger path and its ceiling.** Today, the only widely deployed way for a *team* to use a coding agent is through messenger integrations. Claude Tag, OpenAI Codex, GitHub Copilot, Devin, and Cursor all ship Slack (and in some cases Teams) integrations, and despite independent vendors they converge on one architecture (§2.2): an @-mention triggers the agent; the thread history is captured as context; the work executes in a detached cloud sandbox against a cloned repository; and the result returns as a pull-request link posted to the thread. This design has real virtues — it meets teams where they talk, and modern implementations iterate autonomously against test failures. But its structure imposes three limits that no amount of iteration removes. First, *there is no point of human intervention while the agent runs*: the thread is a trigger and a notification surface, not a workspace, so steering happens before launch or after the PR. Second, *there is no coordination protocol for concurrent instructions*: when Anthropic advertises that "anyone can steer or pick up where someone left off" in a shared channel [2], the documentation is silent on what happens when two people steer at once. Third, *context is trapped at the thread boundary*: the codebase's implicit state, decisions made in other channels, and the agent's own prior work are re-absorbed, if at all, as text. GitHub's documentation is unusually candid about a further consequence: the entire thread is captured and stored in the resulting pull request, which the vendor itself flags as an information-exposure concern [6].

**The obvious alternative and the conventional wisdom against it.** If delegation-by-mention is too detached, why not simply let the team share one live agent session — one conversation context, one working directory, everyone attached? Because the prevailing engineering wisdom holds that shared context forces a choice between two evils. Either you *serialize inference* — a busy gate admits one active run per thread, as in LangGraph's double-texting strategies (reject, enqueue, interrupt, rollback; concurrent execution is not among them) [21] and in the deployed CopilotKit OpenTag framework, which takes a distributed lock per thread and answers concurrent requests with HTTP 409 [22] — reintroducing head-of-line (HOL) blocking, where a latecomer's one-line question waits behind a stranger's long build. Or you *fork the session* — per-user branches, per-task worktrees, optimistic concurrency with post-hoc repair [18, 19, 20] — abandoning the single shared session and paying for it in merge conflicts and context partition: your agent never saw what my agent just did. Prior work in multi-agent coordination explicitly teaches against locking, on the ground that pessimistic serialization blocks inference [19].

**This paper.** We show the choice is false. The two goods that seem to trade off — parallel responsiveness and shared-state integrity — live in different layers, and a contract that treats them separately can have both. Under **parallel inference, serialized side effects**, every user message becomes an independent *turn* that begins LLM inference immediately, regardless of other turns in flight; multiple responses stream simultaneously to all participants, each attributed 1:1 to the message that caused it. Meanwhile *every* side effect — file write, shell execution, package installation, and commit to the shared conversation context — passes through a sandbox-scoped serial executor, refined to a conflict-scoped hierarchical lock so that writes to different files proceed in parallel while shell commands and deletions remain exclusive. Inference, which is read-only and expensive to delay, is never blocked; effects, which are cheap to order and catastrophic to interleave, are never concurrent. The contract deliberately contradicts the standard teaching — it locks, but never in the inference path — and prevents conflicts rather than repairing them: no merge, no rollback, no compensation.

We validate the contract in **Coagora**, a collaborative coding platform organized around a community metaphor: every post is provisioned 1:1 with an isolated sandbox; the sandbox hosts a single shared agent session; any authenticated user may attach, instruct, and interrupt their own turns; unauthenticated visitors may spectate the live token stream. The system is a working implementation, not a simulation: it includes turn-multiplexed streaming, sequence-numbered replay for late joiners, per-user fairness gates, and a deterministic mock-LLM harness that reproduces every number in this paper without API keys.

**Contributions.**

1. **A design space** for multi-user coding-agent interaction with four axes — state locus, concurrency contract, attribution unit, intervention point — that organizes nine deployed and prototype systems and exposes an empty coordinate (§3).
2. **A concurrency contract** — parallel inference, serialized side effects — formalized as snapshot-read/atomic-commit over a single shared context, turnId multiplexing with turn-scoped interrupts, a conflict-scoped hierarchical side-effect lock, and per-user fairness admission (§4).
3. **An open implementation** (Coagora) with a deterministic, key-free reproduction harness and committed raw data (§5).
4. **Empirical validation**: TTFT independence (slope 0.000 vs. 1.000 serial and 1.010 reject-retry; n = 180 ✔), integrity ablation (74% → 0% violation ✔), and characterization of where the benefit collapses as workloads shift from inference-heavy to write-heavy (§6).
5. **A research agenda**: sharing a session eliminates delegation's problems but creates its own — asymmetric grounding between participants and multi-party control arbitration — which we derive from CSCW theory and pose as open problems (§7).

---

## 2 Related Work

### 2.1 One developer, one agent

Studies of AI pair programming uniformly instantiate a dyad. Imai's early comparison found Copilot increased lines produced but degraded quality relative to a human pair [10]; later work showed developers validate AI suggestions less than they validate human partners' [11]. Chen et al. ran the first controlled comparison of assistants against autonomous agents and identified *comprehension of agent behavior* as the central adoption barrier [12]. SWE-chat analyzed 6,000 in-the-wild agent sessions and found users interrupt or redirect the agent in 39% of opportunities while agents almost never pause to ask [13]. These findings anchor our motivation twice over: the interruption rate shows that steering is integral to real agent use — and every one of these studies assumes exactly one human holds the brake. Who brakes when there are three (§7.2)?

### 2.2 Team access via messengers

We surveyed the Slack/Teams integrations of Claude Tag [1, 2], OpenAI Codex [3, 4], GitHub Copilot [5, 6], Devin [7], and Cursor [8] (details and sources in supplementary material). All five implement the same pipeline — mention → thread absorption → cloud sandbox → PR link — differing mainly in identity: Claude Tag gives the agent *its own* accounts (its own GitHub App opens the PR; work is attributed to the organization), Copilot acts *as the requesting user* under that user's repository permissions, and Devin maps Slack identities to per-user accounts by e-mail. Two vendor admissions matter here. GitHub documents that the full thread is captured into the PR and advises DMs when that is unacceptable [6] — thread-as-context is an exposure surface, a risk realized in the wild by indirect prompt injection against Slack AI (MITRE ATLAS case AML.CS0035) [9]. And Claude Tag's "multiplayer" sharing of one instance per channel ships without any published protocol for conflicting concurrent instructions [2]. We characterize this family precisely rather than by strawman: modern background agents *do* iterate autonomously (retrying against test failures), and Devin *does* persist sessions across its web app and Slack; what the family lacks, invariantly, is a human intervention point during execution and a multi-user coordination mechanism.

### 2.3 Multiple humans and an AI — outside coding, or outside the session

The nearest research neighbors approach from three directions, none reaching our setting. In *collaborative writing*, Lehmann et al. embedded agents in a multi-user editor and found, over a week of team use, that teams absorbed the agent into existing collaboration norms — agent profiles behaved as personal workspaces, outputs as shared assets [14]; documents, however, lack the executable state, irreversible side effects, and objective test signals that make coding coordination hard. In *group conversation*, Koala explored when an agent should take the floor in multi-party chat [15], and GroupMemBench observes that agent memory systems are built single-user even as agents are deployed into multi-party channels [16]. In *education*, Daryanto et al. ran the study closest in configuration to ours: twenty participants compared dyadic (1 human + AI) against triadic (2 humans + AI) programming, finding that triads reduced overreliance on AI code — peer visibility made participants feel accountable for understanding suggestions before applying them [17]. We differ on five axes: their setting is collaborative learning, their AI is a suggestion-based assistant rather than an autonomous agent with file and shell effects, the task is a one-off lab exercise, the dependent variables are learning outcomes, and no persistent shared session state exists. Their accountability finding nevertheless supplies a hypothesis we return to in §7.3. Finally, a growing empirical literature studies many humans around one agent's *output* — reviewer engagement on agent-authored pull requests strongly predicts integration [26] — but this is asynchronous, post-hoc collaboration on artifacts; the live session remains single-player. A recent position paper argues that public data on human–agent interaction is scarce overall [27]; multi-human session data does not exist at all.

### 2.4 Concurrency control for agents on shared state

When systems do run agents concurrently over shared mutable state, the dominant paradigms are optimistic. CoAgent lets writes land and repairs misordered ones with saga-style undo, explicitly teaching that pessimistic locking is unsuitable because it blocks inference [19]; DeLM admits parallel inferences into a shared context through a single admission gate [20]; commercial parallel-agent products isolate per branch or worktree and merge later [18]. LangGraph's double-texting enumerates reject, enqueue, interrupt, and rollback for concurrent requests on one thread — concurrent execution is absent [21] — and OpenTag enforces one active run per thread with a distributed lock and 409s [22]. Our contract inverts the teaching these systems share: we lock *only* the effect layer, which inference never waits on, so pessimistic serialization becomes compatible with parallel reasoning; and because effects are ordered before application, the conflict-detection/rollback machinery of the optimistic family is unnecessary rather than merely avoided. Note also the direction of multiplicity: the isolate-and-merge family parallelizes *agents* for one human; we parallelize *humans* over one agent.

### 2.5 Theoretical lenses

Four CSCW traditions frame what sharing a session means. *Grounding* [28]: collaborators accumulate common ground incrementally, at costs set by the medium — with an agent in the loop, grounding becomes a triadic problem, and a session that persists makes the accumulated human–agent ground a first-class asset that late joiners lack (§7.1). *Workspace awareness* [29, 30]: knowing who is doing what, where — structurally isomorphic to telepointers in shared editors, except the agent changes more state faster than any human. *Articulation work* [31]: the coordination labor that makes cooperative work possible, which automation tends to displace rather than remove (§7.4). *Mixed-initiative interaction* [32]: Horvitz's principles govern when an agent should act or ask — but presuppose one human; multi-party settings add *whom to ask*, a dimension the original principles do not address.

---

## 3 A Design Space for Multi-User Coding-Agent Interaction

The systems of §2.2–2.4 look heterogeneous but vary along four discrete axes.

**D1 — State locus.** Where does the agent's session state live relative to the space where humans talk? *Outside* (the conversation triggers a detached executor; state is a copy taken at launch) or *inside* (the conversation surface is a view onto the live session; state is the original).

**D2 — Concurrency contract.** What happens when a second instruction arrives while the agent is busy? *Reject* (busy gate, 409), *serialize* (FIFO single active turn), *batch* (coalesce inputs into one prompt, one fused answer), *isolate-and-merge* (fork per user/task, reconcile later), or *parallel + serialized side effects* (this paper).

**D3 — Attribution unit.** At what granularity can output be traced to the human who caused it? *Pull request*, *task*, or *turn* (every agent message linked 1:1 to a user message).

**D4 — Intervention point.** When can a human affect the work? *Before launch / after completion* only, or *during execution*, and if during — scoped to the whole session or to an individual turn?

Table 1 locates nine systems. [NOTE: render as a proper table/figure for submission.]

| System | D1 locus | D2 contract | D3 attribution | D4 intervention |
|---|---|---|---|---|
| Claude Tag [2] | outside | undefined (shared instance, no protocol) | task → org identity | pre/post |
| Codex Slack [3] | outside | new cloud task per mention | task | pre/post |
| Copilot Slack [5] | outside | serialize per channel default | PR → requester identity | pre/post |
| Devin Slack [7] | outside (synced) | session resume; serial within session | task → mapped user | in-thread replies (coarse) |
| Cursor Slack [8] | outside | new isolated VM per invocation | task | pre/post |
| OpenTag [22] | inside (thread) | **reject** (lock + 409) | message | none while running |
| Parallel-agent tools [18] | outside (per-branch) | isolate-and-merge | branch | per-agent |
| Multi-viewer shared worker (agent-cli family) | inside | **serialize** (shared input queue; first-answer-wins gates) | turn (author labels) | during, session-scoped |
| **Coagora v2 (this paper)** | **inside** | **parallel + serialized side effects** | **turn (replyToId 1:1)** | **during, turn-scoped** |

Two observations. First, the messenger family occupies one tight cluster (outside / task / pre-post) — its limits in §1 are properties of the *coordinates*, not of any vendor's execution. Second, the bottom-right coordinate — state inside, parallel contract, turn attribution, turn-scoped intervention — was empty. The rest of the paper is the design, implementation, and measurement of a system at that coordinate, and the design space predicts the comparison conditions for our evaluation: *serialize* and *reject* are not straw men but the two contracts deployed systems actually use, so §6 measures all three.

---

## 4 The Contract: Parallel Inference, Serialized Side Effects

### 4.1 Model

A **session** is a triple *S = (C, W, T)*: a single shared conversation context *C* (an ordered message list), a single working directory *W* (an isolated sandbox rooted at one path), and a set of turns *T*. Sessions bind 1:1:1 to a *post* — the unit of discovery and access — and to a sandbox whose lifecycle follows the post's. Any number of mutually independent clients attach to one session; the agent's output is generated once and fanned out to all subscribers as an ordered event stream. There is one agent process per session; attaching never spawns a per-user process, VM, or context fork.

The contract is two commitments:

> **(P) Parallel inference.** Every user message becomes an independent turn *t_i*. Inference for *t_i* starts immediately, irrespective of other in-flight turns; responses stream concurrently to all participants.
>
> **(S) Serialized side effects.** Every effect any turn produces — file write, tool execution, and every commit to *C* — is applied through a per-sandbox serial admission mechanism, in completion order, and never concurrently with a conflicting effect.

The asymmetry is principled. Inference is read-only, long (seconds to minutes), and the thing users wait on; blocking it converts one user's task length into another user's latency. Effects are short, cheap to order, and the only channel through which turns can corrupt one another. Serializing the second layer costs little and buys the absence of an entire class of failures; serializing the first costs everything the users came for.

### 4.2 Turns: independence, multiplexing, and 1:1 attribution

Concurrent messages are never batched into one prompt. Batching (D2) produces a fused answer that belongs to no one, destroys per-message inference parameters, and eliminates the per-turn interruption window; we treat attribution as a correctness property, not a UI nicety. Each turn receives a monotonically increasing **turnId** at *dispatch* time (not enqueue time, so identifiers always correspond to work actually issued to the model). Every streamed chunk, tool call, tool acknowledgment, and error is tagged with its turnId; the runtime demultiplexes the agent's single stdout into per-turn sinks, and tool results route back on the composite key (callId, turnId) so that concurrent turns' identically named tool calls cannot cross wires. In persistent storage, each agent reply carries a `replyToId` foreign key to the human message that caused it; the UI renders concurrent streams as separately badged bubbles.

**Turn-scoped interrupts.** An interrupt names a turnId and cancels only that turn: its flag is set, its partial output is finalized and preserved (marked complete rather than discarded), and its slot is reclaimed — while other in-flight turns' inference, streaming, and effects are untouched. Symmetrically, the UI gates each user on *their own* active turn only: someone else's running turn never locks your composer. This is the smallest useful answer to multi-party control (each user commands their own work); §7.2 discusses why it is not the final one.

### 4.3 Shared context: snapshot read, completion-order atomic commit

Turns share one conversation context *C* — this is the point of the system; forking *C* per user would silently reintroduce isolate-and-merge. Two rules keep *C* coherent under concurrency:

- **Snapshot read.** At each inference step, a turn reads an immutable snapshot of *C* as its prompt. In-flight partial output of other turns is never visible.
- **Atomic completion-order commit.** When a step completes, the turn commits its results — the assistant message including its tool_calls, together with *all* corresponding tool-result messages — as one atomic block, through the serial commit chain, in completion order. The block boundary is an invariant: splitting an assistant/tool-results pair would let another turn's commit interleave between them, violating the message-pairing constraint that LLM chat APIs enforce (and rejecting the request outright).

The known cost is bounded staleness: a turn's snapshot may omit effects another turn committed after the snapshot was taken. We disclose rather than solve this (§7.5); the important property is that staleness is a *freshness* limitation, never a *consistency* violation — *C* is always a well-formed transcript some serial history could have produced.

### 4.4 The effect layer: from a global mutex to a conflict-scoped hierarchical lock

Our first implementation serialized effects with one per-sandbox mutex. It was correct — and measurement showed it was too blunt: when *both* concurrent turns modify files, a global mutex collapses the parallel advantage to 1.07× (deterministic harness; 1.35× with a live LLM), because turns that would never touch the same path still queue behind each other (§6.3). The v2 executor therefore serializes at the *conflict* granularity, using a compatibility matrix over classified effect intents:

| Intent pair | Decision | Rationale |
|---|---|---|
| FILE_WRITE/READ(path *p*) vs FILE_WRITE/READ(path *q*), *p* ≠ *q* | **parallel** | disjoint resources |
| FILE_WRITE/READ(*p*) vs FILE_WRITE/READ(*p*) | serial | same resource |
| anything vs SHELL or PACKAGE | serial (exclusive) | a shell's file footprint is statically unknowable (pipes, expansions, subshells) — there is no sound path key |
| anything vs FILE_DELETE | serial (exclusive) | deletion can remove directories; letting `rm -r src/` run beside `write src/x.py` *creates* an ENOENT race class that did not exist under exclusion; deletions are rare, so exclusivity is nearly free |
| unknown intent | serial (exclusive) | safety-first default |

Path keys are normalized (separators unified; case-folded on case-insensitive filesystems, since `A.txt` and `a.txt` must collide). Admission is **strict FIFO per sandbox — no overtaking**: if the queue head is incompatible with the running set, the pump stops rather than scanning past it. This deliberately sacrifices some concurrency for a hard fairness guarantee — a stream of file operations can never starve a waiting shell command. A configuration switch (`LOCK_SCOPE=sandbox`) restores the global mutex, serving as both an instant rollback path and the ablation arm for §6.

Contrast with the optimistic family (§2.4) is now precise. CoAgent's teaching — "locks block inference" — is true of locks *in the inference path* and irrelevant here: inference never acquires this lock. And because incompatible effects are ordered *before* application, there is nothing to detect, roll back, compensate, or merge; the logical outcome for same-resource writes is last-wins *by construction of order*, not by conflict resolution.

### 4.5 Admission: cap, fair queue, one turn per user

Unbounded parallelism converts user enthusiasm into unbounded token cost. The session enforces a concurrency cap (default 4 in-flight turns). Beyond the cap, inputs are neither rejected nor batched: they enter a fair queue scanned head-to-tail for the first *eligible* item, where eligibility means the submitting user has no active turn (**one active turn per user** — parallelism exists *between* users, not within one user's backlog, so a single user queueing five requests cannot monopolize slots). System-initiated turns are exempt from the per-user gate. Queued items never delay a different user's immediate dispatch. §6 shows why the polite-sounding alternative — reject-and-let-them-retry — is quantitatively the worst contract of the three.

### 4.6 Session state and lifecycle

Session state is *count-based*: RUNNING iff at least one turn is in flight, IDLE iff zero — there is no single "the" active turn whose completion defines idleness, and the transition to IDLE is gated on the last turn to avoid mid-session flicker. At zero activity the sandbox may suspend (process terminated, directory and files preserved) and later resume on the next attach or message. The parallel path is an **opt-in gate fixed once at post creation** and immutable thereafter: a sandbox is created serial or concurrent and never switches, so serial and parallel turns never coexist in one session and context-coherence rules are never renegotiated mid-life. Corrupted or missing gate metadata fails safe to serial.

### 4.7 Ordering, replay, and idempotence

Every persisted bubble — human message, agent reply, tool call, tool result, system notice — carries a per-post monotonically increasing sequence number `seq`, allocated inside the same database transaction that persists the row and backstopped by a uniqueness constraint. `seq` is the single source of truth for ordering, reconnection replay, and idempotence: a client reconnecting with its last-seen id receives a deterministic replay of everything missed, with turn attributions intact; client-supplied idempotency keys make message submission safe to retry. Events are fan-out notifications over this durable order, not the order itself — the server holds no client state that matters.

---

## 5 Implementation

Coagora is ~5,600 lines of TypeScript (Node 20, Fastify) plus a React front end; everything reported here is in the public repository with committed raw data. We highlight decisions a replicator would need.

**Transport: SSE, not WebSocket.** Downstream is a single Server-Sent-Events stream per post; upstream is plain HTTP POST. SSE's browser-native auto-reconnect with `Last-Event-ID` gives replay-on-reconnect *for free* against the `seq` order (§4.7) — the server implements replay as a database range read, holds no per-client cursor, and needs none of the bidirectional session machinery WebSockets would demand. Spectators (read-only participants) are ordinary unauthenticated subscribers.

**Process split: the worker proposes, the server disposes.** The LLM loop runs in a child process that owns *no* filesystem or shell access. It emits tool *intents* on stdout; the parent server classifies each intent, acquires the conflict-scoped lock (§4.4), executes the effect behind three guards, and returns an acknowledgment routed by (callId, turnId). The guards: (1) *path confinement* — every path is normalized and resolved through the nearest existing ancestor's realpath before a root-prefix check, defeating `..` traversal, absolute-path injection, and symlink escape while still permitting creation of not-yet-existing files; (2) *deny-by-default environment* — child processes receive an allowlist of benign variables only. This rule was learned, not assumed: an early build inherited the server's environment into tool shells, so `echo $API_KEY` streamed the provider key to every SSE subscriber. The fix is enforced by regression tests and a CI grep gate over the event schema; event constructors accept only enumerated safe fields, making key leakage structurally inexpressible rather than merely forbidden; (3) *resource limits* — wall-clock timeout with full process-tree kill and a per-sandbox process cap. We claim exactly this much: directory isolation plus these guards. Container, cgroup, and network isolation are not implemented (a Docker proof-of-concept exists outside the product), and we do not simulate limits we cannot enforce portably.

**Keys are server-internal.** Provider credentials live only in server secrets, injected into the worker's environment; they appear in no response, log, event, or client. Session records store the model name only.

**Failure containment.** Turns are launched fire-and-forget from the message route (the 201 returns immediately). An early version let an exception in that path — a post deleted mid-turn — surface as an unhandled rejection and take down the entire server; the failure was *discovered by the benchmark harness* (39 of 180 runs cascading) and is now absorbed by a top-level wrapper that treats record-gone as a normal early exit. Worker death sweeps all four turn registries (legacy active/queue, concurrent active/queue) so no promise hangs; interrupts suppress stale completions on both sides of the process boundary.

**Determinism for science.** A stub mode replaces the LLM with a deterministic echo-with-delay model whenever keys are absent or tests are running. Every benchmark in §6 runs against a local mock OpenAI-compatible server with scripted latencies: identical inputs produce identical timelines, the full suite runs without network or keys, and the paper's figures regenerate from committed constants — the demo clip and the plots cannot disagree with the tables.

---

## 6 Evaluation

We evaluate four questions. **Q1** Does the contract eliminate head-of-line blocking — is a user's latency independent of others' work? **Q2** What do the alternative deployed contracts cost? **Q3** Does effect serialization actually prevent corruption, and what does lock granularity buy? **Q4** Where does the benefit collapse?

**Setup.** All headline numbers use the deterministic harness (§5): mock LLM with scripted task lengths, real server, real sandboxes, real locks — only the model is simulated, so measured intervals are dominated by the system under test, and every run is reproducible from the repository without credentials. Spot checks against a live LLM appear where noted. Raw JSONL for every figure is committed. ✔

### 6.1 Q1/Q2 — Head-of-line blocking under three contracts ✔

Protocol: user A starts a task of length *L* ∈ {2 s, 6 s, 15 s}; one second later user B sends a one-line question; we measure B's time-to-first-token. Contracts: *serial* (FIFO, the v0.1 path), *reject-and-retry* (busy gate returns 409; client retries with backoff — the OpenTag/double-texting family), *parallel* (v2). n = 180 total (20 per cell).

| Contract | L = 2 s | L = 6 s | L = 15 s | slope dTTFT/dL |
|---|---|---|---|---|
| Serial (FIFO) | 1.93 s | 5.93 s | 14.93 s | 1.000 |
| Reject + retry | 2.26 s | 6.30 s | 15.39 s | **1.010** |
| **Parallel (ours)** | **0.24 s** | **0.24 s** | **0.24 s** | **0.000** |

The number that matters is the *slope*, not the ratio. At slope 1.000, every second of your build is a second of my waiting; the 63.5× advantage at L = 15 s is merely where our measurements stop, not where the effect does. The parallel contract's slope of 0.000 means B's latency is a property of B's own request — the definition of independence. The reject contract deserves emphasis because it is what deployed multi-user frameworks actually ship: it is *strictly worse than serial* (retry backoff adds dead time on top of the same wait), so a busy gate is not a cautious middle ground but the bottom of the ranking. [TODO: add L = 30 s cell per experiment plan P1.]

### 6.2 Q3 — Integrity: what serialization prevents ✔

Ablation: two concurrent turns write the same file with distinguishable markers, with the serial executor bypassed versus enabled. Bypassed: **74%** of runs showed integrity violations (both writers' markers interleaved in the final file; in all three inspected corruption cases the file was unusable). Enabled: **0%** across all runs — with no merge step, no conflict dialog, and no rollback machinery, because concurrent same-file writes never physically occur. This is the ablation that shows the lock is load-bearing, not ceremonial.

### 6.3 Q4 — Where the benefit collapses, and what granularity recovers ✔ / [TODO]

Honesty requires the boundary. When *both* concurrent turns are write-heavy, the global-mutex executor serializes nearly everything and the end-to-end advantage over the serial contract collapses to **1.07×** (deterministic; **1.35×** live-LLM) — the cost of choosing prevention over merging, paid exactly when workloads conflict. This measurement motivated the v2 conflict-scoped lock (§4.4), which restores parallelism for the common case of concurrent turns touching *disjoint* files while keeping shells and deletions exclusive. [TODO — experiment P2: full grid over effect ratio m ∈ {0, 25, 50, 75, 100}% × conflict rate c ∈ {0, 50, 100}% × lock scope {sandbox, conflict}; hypothesis: conflict-scoped locking preserves the parallel advantage at any m when c = 0, and the collapse boundary tracks m·c. This is the heatmap figure.] [TODO — P4: fairness under N ∈ {2,4,8} users, Jain index; P5: 200-run interrupt-isolation statistics.]

### 6.4 Correctness, UI, and fault isolation ✔

Thirty-five backend test suites cover the concurrency machinery (turn multiplexing, cap and per-user gates, lock scope, worker concurrency, replay); an asserting browser E2E drives two real clients through concurrent turns and fails on any cross-turn UI attribution error. The fire-and-forget failure of §5 — found by the harness, fixed, and regression-tested — doubles as evidence that the evaluation apparatus exercises the system adversarially rather than along a happy path.

**Threats to validity.** Mock-LLM timings exclude provider-side variance (mitigated by live-LLM spot checks and by reporting the deterministic/live pair where both exist); results come from one implementation on one host (the contract, however, is architecture-level); token-cost overhead of N-parallel inference is real and quantified separately [TODO — P6: prompt-token multiplier and cache-adjusted effective cost]; and our workloads are synthetic pending the field-log mix distribution [TODO — S1].

---

## 7 Discussion: What Sharing a Session Creates

The contract removes delegation's structural limits. It would be dishonest to stop there: co-presence in one live session generates problems that detached delegation never had. We derive four from the theory of §2.5 — they are this paper's research agenda, not its solved claims.

### 7.1 Asymmetric grounding

Clark and Brennan's account of grounding [28] assumed the parties accumulate common ground *together*. A persistent shared agent session breaks the symmetry: user A and the agent build thirty minutes of working context — decisions taken, approaches abandoned, files touched — and user B attaches cold. B can replay the transcript (§4.7 guarantees they *can*), but replay is access, not understanding; the session's common ground is now an artifact with an owner gradient. Our seq-ordered, turn-attributed transcript is the raw material for catch-up interfaces (summaries, decision digests, per-file "what happened here" views), but which of these closes the gap — and whether closing it is even always desirable — is an open empirical question (planned study S2: staged mid-session joins under three catch-up treatments, measuring comprehension, time-to-first-productive-intervention, and re-litigation of settled decisions).

### 7.2 Multi-party control arbitration

Horvitz's mixed-initiative principles [32] tell an agent when to act versus ask — one human presumed. With several, every control verb acquires an indirect object: *whose* approval, *whose* interrupt, *whose* undo? Our shipped policy is deliberately minimal: per-user turn ownership (you may interrupt yours; you cannot touch mine; my turn never locks your composer). It is one point in a policy space that also contains role-based override (the incident commander stops anything), quorum gates for destructive effects, and social-transparency approaches (anyone may stop anything, visibly). SWE-chat's finding that solo users brake 39% of the time [13] sharpens the question: in a shared session, is braking redundant (someone will catch it), diffused (everyone assumes someone else will — the bystander pattern), or amplified (more eyes, more brakes)? The turnId mechanism makes any of these policies implementable; which of them *works* is study S3.

### 7.3 Peer accountability at agent speed

Daryanto et al. found that when a peer can see your AI use, you verify suggestions more before applying them [17] — in a suggestion-based, human-paced setting. An autonomous agent changes the regime: effects land at machine speed across many files, and the thing peers observe is a stream, not a diff under a cursor. Does visibility still purchase verification when there is more to verify per minute than a human can read? Our fan-out design makes every participant a potential verifier by default; whether that produces Daryanto's accountability or mere alarm fatigue is precisely testable in this system and unknown.

### 7.4 Articulation work is displaced, not destroyed

Schmidt and Bannon predicted that coordination technology relocates articulation work rather than eliminating it [31]. Delegation's articulation work is visible in the field: tracking which channel launched which task, deduplicating parallel mentions, reconciling three PRs that each half-solve the bug. The shared session absorbs much of this into mechanism — the queue orders, the lock serializes, `replyToId` attributes. New work appears in its place: deciding when to inject a steer into a running turn, negotiating the cap, reading the swim of concurrent streams. We conjecture the exchange is favorable (mechanized ordering is cheaper than social ordering) but hold, with the theory, that the correct claim is *displacement* — and that measuring the new work (S1 field logs) matters more than celebrating the old work's disappearance.

### 7.5 Limitations

Honest boundaries of the current system and results. (1) *Token cost*: N parallel turns pay ~N inference costs over overlapping snapshots; provider-side prompt caching mitigates but does not erase this [TODO: P6 quantifies]. (2) *Context capacity*: one shared context fills N times faster; the platform has no compaction (a companion CLI system in our group implements token-budget compaction — integration is future work). (3) *Logical last-wins*: serialization prevents physical corruption, not semantic conflict — two turns may disagree about what a file should contain, and the later one wins; surfacing semantic conflicts to humans is open. (4) *Lock envelope*: effects of processes a tool detaches into the background escape the serial executor. (5) *Snapshot staleness* (§4.3). (6) *Single-instance*: pub/sub, locks, and registries are in-process; the pub/sub seam is interface-ready for a distributed backend but unimplemented. (7) *Isolation depth*: directory-level, not container-level (§5); the open-participation model (any authenticated user may execute code in any post's sandbox) is intended for the community setting and demands the stated threat model. (8) *Steer-with-interrupt* is unsupported on concurrent turns (pure cancellation only). (9) Tool bubbles attribute to turns exactly in serial mode but heuristically (nearest preceding reply) under concurrency — the 1:1 attribution claim is scoped to agent replies.

---

## 8 Conclusion

Every deployed coding agent is single-player, and the industry's team workaround — delegation by @-mention — structurally forecloses the two things teams need most from a collaborator: the ability to intervene while work happens, and a rule for what happens when two people speak at once. The conventional wisdom held that fixing this inside a shared session forces serialization or forking. We showed it does not. Locating responsiveness and integrity in different layers — inference parallel and never blocked, side effects serialized and never concurrent — yields a session that multiple humans genuinely share: everyone's question answered at its own speed (TTFT slope 0.000), no file ever corrupted by a race (74% → 0%), every answer attributed to its asker, and every participant able to stop their own work without touching anyone else's. The design space of §3 says this coordinate was empty; the system says it is habitable; the measurements say it is worth inhabiting.

What we did not solve, we named. A shared session mints asymmetric grounding, demands control arbitration policies richer than "own your turn," and tests whether peer accountability survives machine speed. These are not flaws in multiplayer agents — they are the field that opens once agents become multiplayer. The system, harness, and raw data are available for that work, including ours.

---

## References

[NOTE: normalize to venue format; ⚠ = verify against original before camera-ready (per research doc 04 §9).]

1. Anthropic. *Introducing Claude Tag.* 2026. https://www.anthropic.com/news/introducing-claude-tag
2. Anthropic. *What is Claude Tag.* Support documentation, 2026. https://support.claude.com/en/articles/15594475
3. OpenAI. *Codex — now generally available.* 2026. https://openai.com/index/codex-now-generally-available/
4. OpenAI. *Codex Slack integration.* Documentation, 2026. https://learn.chatgpt.com/docs/third-party/slack
5. GitHub. *Integrate Copilot cloud agent with Slack.* Documentation, 2026. https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/integrate-cloud-agent-with-slack
6. GitHub. *About Copilot integrations* (thread-capture notice). Documentation, 2026.
7. Cognition. *Devin Slack integration.* Documentation, 2026. https://docs.devin.ai/integrations/slack
8. Cursor. *Cursor in Slack.* Documentation, 2026. https://cursor.com/docs/integrations/slack
9. MITRE ATLAS. *Case study AML.CS0035: Data exfiltration from Slack AI via indirect prompt injection.* 2024.
10. S. Imai. *Is GitHub Copilot a substitute for human pair-programming?* ICSE Companion 2022.
11. *From developer pairs to AI copilots.* arXiv:2506.04785, 2025.
12. V. Chen, A. Talwalkar, J. Brennan, G. Neubig. *Code with me or for me? How increasing AI automation transforms developer workflows.* arXiv:2507.08149, 2025.
13. Baumann et al. *SWE-chat: Coding agent interactions from real users in the wild.* arXiv:2604.20779, 2026.
14. Lehmann, Shauchenka, Buschek. *Collaborative document editing with multiple users and AI agents.* CHI 2026. arXiv:2509.11826.
15. *Controlling AI agent participation in group conversations: A human-centered approach.* IUI 2025.
16. ⚠ *GroupMemBench: Benchmarking LLM agent memory in multi-party conversations.* arXiv:2605.14498, 2026.
17. Daryanto et al. *Human-human-AI triadic programming.* arXiv:2601.12134, 2026.
18. Industry parallel-agent tooling (per-branch/worktree isolation with post-hoc merge). [NOTE: pick 1–2 citable exemplars.]
19. *CoAgent* — optimistic write-ordering with saga-style repair for concurrent agents on shared state. [NOTE: complete citation from patent NPL list, 비특허문헌 1.]
20. *DeLM* — admission-gated parallel inference into shared context. [NOTE: complete citation, 비특허문헌 2.]
21. LangChain. *LangGraph double-texting strategies.* Documentation.
22. CopilotKit. *OpenTag — threads & persistence architecture* (single active run per thread; 409 on concurrency). Documentation, 2026.
23. Slack Engineering. *Coding agents in Slack.* 2026. https://slack.com/blog/developers/coding-agents-in-slack
24. GitHub Next. *Ace: Agent Collaboration Environment.* Technical preview, 2026. https://ace.githubnext.com/ (accessed 2026-08-05).
25. *Claude Code issue #60082: multi-user shared sessions.* GitHub, 2026.
26. *When AI teammates meet code review.* MSR 2026. arXiv:2602.19441.
27. ⚠ Z. Z. Wang et al. *Position: Humans are missing from AI coding agent research.* 2026. [NOTE: verify venue/author list from PDF.]
28. H. H. Clark, S. E. Brennan. *Grounding in communication.* In Perspectives on Socially Shared Cognition, 1991.
29. P. Dourish, V. Bellotti. *Awareness and coordination in shared workspaces.* CSCW 1992.
30. C. Gutwin, S. Greenberg. *A descriptive framework of workspace awareness for real-time groupware.* JCSCW, 2002.
31. K. Schmidt, L. Bannon. *Taking CSCW seriously: Supporting articulation work.* JCSCW 1(1), 1992.
32. E. Horvitz. *Principles of mixed-initiative user interfaces.* CHI 1999.

---

## Appendix A — Artifact availability (draft)

The implementation, the deterministic benchmark harness (mock LLM with scripted latencies), all raw measurement files (JSONL), the CDF/figure generation scripts, and the browser E2E suite are available in the repository. Every number in §6 marked ✔ regenerates via `npm run bench:e2` / `bench:e1` without API keys or network access. [NOTE: replace with anonymized artifact link for double-blind review.]
