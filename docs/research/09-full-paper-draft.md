# Coagora: Multiplayer Coding Agents via Concurrency Contracts for Synchronous Multi-User Sharing of a Single LLM Agent Session

> Full paper draft v1.0-wip (2026-08-08). Round-1 review response Phases 0–3 (`14-review-response-plan.md`) and round-2 Phases A–C plus the mock-versus-live audit (`16-review-response-plan-2.md`, `17-mock-vs-live-audit.md`) are applied: every live measurement has been repeated on the repaired step seam, the realistic-workload base rate is measured, and the four experiments whose mock could misrepresent them now carry live arms. The change history for v0.3 through v1.0-wip is in `09-CHANGELOG.md`. The venue is decided: **CHI Papers**, and the anonymized submission derivative is `21-chi-submission-draft.md` (this file remains the working master; every reference author list is now source-verified). Remaining before submission: the first-use study reserved in §6.11 (protocol in `18-first-use-study-protocol.md`, run kit in `22-study-run-kit.md`, awaiting participants) and integration of its results into the derivative.
> Status notes for authors appear as `[NOTE: …]` and `[TODO: …]` and must be removed before submission.
> Numbers marked ✔ are backed by committed raw data in `bench/multiuser/out/`.

---

## Abstract

AI coding agents are still designed for one person at a time. Teams can delegate work through messenger integrations, but those integrations launch detached tasks: people cannot intervene while the agent is running, and the system provides no clear rule for simultaneous instructions. A live shared session appears to offer only two choices. It can serialize inference, making a short question wait behind a long build, or it can fork the session and lose a single shared context.

We show that this trade-off is unnecessary. Our contract, **parallel inference with serialized side effects**, separates the slow read-only work from the short state-changing work. Each message starts an independent turn immediately over the shared context. File writes, shell commands, and context commits pass through a conflict-scoped lock. Users therefore do not wait for one another's inference, while conflicting effects never run at the same time.

We place this contract in a four-axis design space covering nine systems and implement it in Coagora. Because Coagora already ships a serial mode, the experimental arms differ only by a runtime switch. In a deterministic harness, the second user's time-to-first-token no longer depends on the first user's task length: its slope is 0.00, compared with 1.03 for both serialization and the reject gate used by deployed frameworks. In a forced-overlap ablation, locking reduces torn writes from 8.2% to zero without merge or rollback. The ranking also holds with a live model, where parallel execution is 3.5× faster and uses 1.49× as many input tokens; this ratio does not grow with history length.

A shared transcript introduces a different problem: one turn may follow another participant's instruction. We observed this in 56 of 80 live turns, including turns whose requests were clearly different. Adding each turn's own request to its prompt eliminated the problem in all 80 turns on one model, but only reduced it on a second. We therefore present prompt scoping as a measured mitigation, not a guarantee. We close by identifying two broader questions created by live sharing: asymmetric grounding and multi-party control arbitration.

[TODO after the first-use study: add one sentence reporting the central human finding from §6.11. Use an observed awareness, detection, coordination, or deployment-fit result; do not restate a systems metric from §6.1–§6.10.]

---

## 1 Introduction

Coding agents now do much more than suggest completions. They read repositories, edit files, run shell commands and tests, and open pull requests with limited supervision [1, 2]. Controlled studies show that they can complete tasks users could not finish alone [2]. Large-scale telemetry also reports that agents wrote all the code in 41% of real sessions [3]. Yet the interaction model behind this progress remains almost entirely single-player: **one human, one agent**.

Software development is rarely single-player. Industry is beginning to recognize the mismatch. GitHub Next's Ace prototype argues that single-player agents create wasted work, coordination debt, and misaligned output [4]. Claude Code users have likewise requested Google-Docs-style shared sessions [5]. Practice is moving ahead of research: we know of no published study in which several people synchronously share one autonomous coding-agent session (§2.3), and no systems paper that explains how such a session should coordinate their actions.

**How teams share agents today.** The main deployed option is a messenger integration. Claude Tag, OpenAI Codex, GitHub Copilot, Devin, and Cursor all connect to Slack, and some also connect to Teams. Despite coming from different vendors, they use a similar pipeline (§2.2): an @-mention starts a task, the thread becomes context, the agent works in a detached cloud sandbox on a cloned repository, and a pull-request (PR) link returns to the thread. This approach is useful. It starts where teams already communicate, and modern agents can iterate on their work after tests fail.

The same architecture has three structural limits. First, people cannot intervene while the task is running; the thread is a launch and notification surface, not a live workspace. Second, the integrations publish no coordination rule for simultaneous instructions. Anthropic, for example, says that anyone can steer a shared channel, but does not explain what happens when two people steer at once [6]. Third, context stops at the thread boundary. Decisions from other channels, implicit repository state, and the agent's earlier work must be reconstructed as text. GitHub also notes that the full thread is stored in the resulting PR, creating an information-exposure risk [7].

**Why not share one live session?** A team could instead attach everyone to one conversation context and one working directory. Existing designs suggest that this requires one of two compromises.

The first is to *serialize inference*. A busy gate allows only one active run per thread. LangGraph's double-texting strategies offer reject, enqueue, interrupt, or rollback, but not concurrent execution [8]. CopilotKit's OpenTag similarly locks each thread and returns HTTP 409 to concurrent requests [9]. This creates head-of-line (HOL) blocking: a short question from one person waits behind another person's long build.

The second is to *fork the session*. Each user or task gets a branch or worktree, and conflicts are repaired or merged later [10, 11]. This preserves concurrency but gives up the single shared session. It introduces merge work and partitions context: one agent does not see what another agent has just done. Prior multi-agent work therefore argues against pessimistic locking because it assumes that the lock must also block inference [11].

**Our approach.** We separate responsiveness from integrity because they belong to different layers. Under **parallel inference, serialized side effects**, every user message starts an independent *turn* immediately. Responses stream at the same time and remain linked 1:1 to the messages that caused them.

Only state-changing operations are ordered. File writes, shell commands, package installations, and commits to the shared conversation context pass through a serial executor. A conflict-scoped hierarchical lock lets operations on different files proceed together, while shell commands, deletions, and operations on the same file remain exclusive. Inference is read-only and slow, so it never waits for this lock. Effects are brief but dangerous to interleave, so they do.

The key difference from prior pessimistic designs is therefore the placement of the lock. We lock below inference and prevent conflicts before they happen. No merge, rollback, or compensation step is required.

**Where the evidence comes from.** We implement the contract in **Coagora**, an open-source coding agent that already supported shared viewing and input before this work. One process served one conversation context and one working directory to any number of browsers on a local-area network (LAN). Its limitation was concurrency: a shared queue admitted only one active turn at a time. That serial contract remains the shipped default.

This existing implementation gives us a strong baseline. The serial arm is Coagora's production path, not a simplified reconstruction of another system. The parallel and reject contracts run beside it through opt-in gates while keeping the transport, context manager, tools, and instrumentation unchanged. The comparisons in §6 therefore change the contract, not the surrounding system.

**Contributions.**

1. **A design space** for multi-user coding-agent interaction with four axes (state locus, concurrency contract, attribution unit, intervention point) that organizes nine deployed and prototype systems and exposes an empty coordinate (§3).
2. **A concurrency contract**, parallel inference with serialized side effects, formalized as snapshot-read/atomic-commit over a single shared context, turn-id multiplexing with turn-scoped interrupts, a conflict-scoped hierarchical side-effect lock, and per-user fairness admission (§4).
3. **An implementation inside a working shared-session agent** (§5), with the lock scope, the concurrency contract, the fairness gate, and per-turn prompt scoping each exposed as a switch, so every claim in §6 has an ablation arm that runs on the same binary; plus a deterministic, key-free reproduction harness and committed raw data.
4. **A within-system comparison of three deployed contracts** (§6). We measure TTFT independence (slope 0.00 for parallel, versus 1.03 for serial and reject-retry; 236 of 240 runs analysed ✔), integrity under forced overlap (8.2% torn writes without locking, 0% with locking ✔), and the point at which effect-heavy work erodes parallelism. Separate experiments show that serialization cost follows boundary density, reject cost follows the retry interval, and the parallel result holds at 4× the user count (✔). We also test replay, concurrent context compaction, structural attribution, per-user fairness, suspend/resume durability, and live-model behavior. The live model shows a 1.49×, rather than N×, token premium (§6.10).
5. **A research agenda**: sharing a session eliminates delegation's problems but creates its own, asymmetric grounding between participants and multi-party control arbitration, which we derive from Computer-Supported Cooperative Work (CSCW) theory and pose as open problems (§7).

---

## 2 Related Work

### 2.1 One developer, one agent

Research on AI pair programming almost always studies a dyad: one developer and one AI. Imai found that Copilot increased the amount of code produced but reduced quality relative to human pairing [12]. Later work showed that developers scrutinize AI suggestions less than suggestions from human partners [1]. Chen et al. compared assistants with autonomous agents and identified understanding agent behavior as the main barrier to adoption [2]. SWE-chat studied 6,000 real agent sessions and found that users interrupt or redirect the agent in 39% of available opportunities, while agents rarely stop to ask questions [3].

These results motivate our work in two ways. Steering is a normal part of agent use, not an edge case. At the same time, every study assumes that one person controls the brake. A shared session must answer what happens when several people can use it (§7.2).

### 2.2 Team access via messengers

We surveyed the Slack and Teams integrations of Claude Tag [13, 6], OpenAI Codex [14, 15], GitHub Copilot [16, 7], Devin [17], and Cursor [18]. All five follow the same broad pipeline: mention → thread capture → cloud sandbox → PR link. They differ mainly in identity. Claude Tag gives the agent its own account and attributes work to the organization. Copilot acts with the requesting user's repository permissions. Devin maps each Slack identity to a user account by email.

Two documented details reveal the limits of this pattern. GitHub stores the full thread in the PR and recommends direct messages when that exposure is unacceptable [7]. Indirect prompt injection against Slack AI has already demonstrated the practical risk of treating a thread as context [19]. Claude Tag describes one shared instance per channel as “multiplayer,” but publishes no protocol for conflicting simultaneous instructions [6].

These systems are capable background agents: they can retry after test failures, and Devin can persist a task between its web app and Slack. Their shared limitation is narrower. People cannot intervene during execution, and the system does not coordinate multiple users acting at once.

### 2.3 Multiple humans and an AI: outside coding, or outside the session

The closest research comes from collaborative writing, group conversation, and education. In collaborative writing, Lehmann et al. placed agents in a multi-user editor for a week. Teams incorporated the agents into existing norms: agent profiles became personal workspaces, while their outputs became shared assets [20]. Coding differs because its state is executable, its effects can be hard to reverse, and tests provide objective feedback.

In group conversation, Houde et al. studied an agent joining group ideation. They developed controls for when, what, and where the agent should respond [21]. GroupMemBench likewise observes that memory systems remain designed for one user even when agents appear in multi-party channels [22]. This work studies participation and memory, but not concurrent execution over a shared coding workspace.

The closest configuration appears in education. Daryanto et al. compared dyadic programming (one human and one AI) with triadic programming (two humans and one AI) across twenty participants [23]. Triads relied less blindly on AI code because peer visibility encouraged participants to understand suggestions before applying them. Their setting still differs from ours: it concerns collaborative learning, uses a suggestion-based assistant rather than an autonomous agent with file and shell access, evaluates a one-off lab task and learning outcomes, and has no persistent shared session. Its accountability result nevertheless motivates the question in §7.3.

Other work studies many people around an agent's *output*. For example, reviewer engagement strongly predicts whether an agent-authored PR is integrated [24]. This collaboration is asynchronous and happens after the agent has worked; the live session itself remains single-player. Public data on human-agent interaction is scarce in general [25], and multi-human session data is not yet available.

### 2.4 Concurrency control for agents on shared state

Systems that run several agents over shared mutable state usually use optimistic coordination. CoAgent allows writes and repairs bad ordering with saga-style undo; it rejects pessimistic locking because such locking would block inference [11]. STORM tracks versions and detects conflicts when several agents write to one codebase [26]. Commercial tools commonly isolate each agent in a branch or worktree and merge later; Claude Code documents this pattern directly [10].

DeLM comes closest to our design because its agents run in parallel over one shared context [27]. However, it verifies whether content is sufficiently supported before admitting that content to the context. It does not order concurrent work. Its direction of multiplicity is also different: many agents serve one goal, rather than many humans sharing one agent.

LangGraph offers reject, enqueue, interrupt, and rollback when a second request reaches a busy thread; it does not offer concurrent execution [8]. OpenTag permits one active run per thread and returns 409 for another request [9]. We evaluate the first two policies directly. *Reject* becomes our reject-and-retry arm, while *enqueue* is behaviorally the same as our serial arm. Interrupt and rollback instead change what happens to the first user's work, so we treat them as steering policies and do not measure them here.

Our contract puts pessimistic ordering only in the effect layer. Inference never takes that lock, so reasoning can remain parallel. Because conflicting effects are ordered before they run, the system does not need later conflict detection, rollback, or merging. The direction of sharing also matters: isolate-and-merge tools parallelize agents for one person; we parallelize people over one agent.

Three adjacent lines confirm the coordinate stays empty from other directions. On the *serving* side, fairness schedulers such as Justitia allocate GPU time across task-parallel agents with virtual-time queuing [28]: fairness among *tasks or agents*, not among the humans sharing one session. Per-user fairness is of course routine in multi-tenant serving, where API gateways apply per-client rate limits and token buckets; the claim we make for our admission gate (§4.5) is narrower than "first user-level fairness mechanism," and is this: we have found no prior fairness mechanism at *turn* granularity **within a single shared agent session**, where the contended resource is that session's own concurrency slots and the competing parties are co-present humans in one conversation rather than isolated tenants.

On the *context* side, parallel compaction pipelines summarize long single-agent histories by fanning blocks out concurrently [29]: the compaction itself is parallel, but no prior work compacts a conversation *while concurrent user turns are appending to it*, the problem §6.6 measures. On the *multi-user* side, recent work on multi-user LLM agents studies permission hierarchies and privacy across principals [30], and ProACT detects coordination breakdowns among collaborating users [31]. Both treat multi-user *policy*, neither concurrent execution; they are complements, not competitors, to a concurrency contract.

**Relation to classical concurrency control.** The mechanisms in §4 are established database techniques. Reading an immutable snapshot and committing atomically in completion order is snapshot isolation [32]. It inherits snapshot isolation's anomalies, especially write skew; §4.3 and Limitation 3 explain the semantic form of that problem in our setting. Locking classified resources through a compatibility matrix follows granular and intention locking [33]. Making shell commands and deletions workspace-exclusive follows the same coarse-grained fallback for operations whose resource set cannot be known in advance. The pessimistic-versus-optimistic trade-off and last-writer outcomes are also well established [34, 35].

Our contribution is where we place these mechanisms. Prior agent systems apply concurrency control where the model runs and conclude that pessimistic locking is too expensive because it stalls inference [11]. We apply it below inference, only where state changes. The techniques are classical; the systems question is whether this placement works when LLM turns behave like transactions and people watch them in real time. Section 6 measures the answer.

### 2.5 Theoretical lenses

Four CSCW traditions frame what sharing a session means. *Grounding* [36]: collaborators accumulate common ground incrementally, at costs set by the medium; with an agent in the loop, grounding becomes a triadic problem, and a session that persists makes the accumulated human-agent ground a first-class asset that late joiners lack (§7.1). *Workspace awareness* [37, 38]: knowing who is doing what, where; structurally isomorphic to telepointers in shared editors, except the agent changes more state faster than any human. *Articulation work* [39]: the coordination labor that makes cooperative work possible, which automation tends to displace rather than remove (§7.4). *Mixed-initiative interaction* [40]: Horvitz's principles govern when an agent should act or ask, but they presuppose one human; multi-party settings add *whom to ask*, a dimension the original principles do not address.

---

## 3 A Design Space for Multi-User Coding-Agent Interaction

The systems in §2 look different, but four questions are enough to distinguish them. We derived these questions from documentation rather than choosing them in advance. For each system, we recorded what happens when a second person speaks while the agent is already working. We then grouped the answers until every system had a clear position.

Table 1 maps nine systems, including two Coagora modes. It is a map of systems we could document, not a claim that the four-axis space is densely populated. Evidence also varies by axis. Vendors usually document their concurrency behavior (D2), while the intervention point (D4) often has to be inferred from the interface.

**D1. State locus.** Where does the agent's session state live relative to the space where humans talk? *Outside* (the conversation triggers a detached executor; state is a copy taken at launch) or *inside* (the conversation surface is a view onto the live session; state is the original).

**D2. Concurrency contract.** What happens when a second instruction arrives while the agent is busy? *Reject* (busy gate, 409), *serialize* (first-in-first-out (FIFO) single active turn), *batch* (coalesce inputs into one prompt, one fused answer), *isolate-and-merge* (fork per user/task, reconcile later), or *parallel + serialized side effects* (this paper).

**D3. Attribution unit.** At what granularity can output be traced to the human who caused it? *Pull request*, *task*, or *turn* (every agent message linked 1:1 to a user message).

**D4. Intervention point.** When can a human affect the work? *Before launch / after completion* only, or *during execution*, and if during, scoped to the whole session or to an individual turn?

Table 1 locates nine systems. [NOTE: render as a proper table/figure for submission.]

| System | D1 locus | D2 contract | D3 attribution | D4 intervention |
|---|---|---|---|---|
| Claude Tag [6] | outside | undefined (shared instance, no protocol) | task → org identity | pre/post |
| Codex Slack [14] | outside | new cloud task per mention | task | pre/post |
| Copilot Slack [16] | outside | serialize per channel default | PR → requester identity | pre/post |
| Devin Slack [17] | outside (synced) | session resume; serial within session | task → mapped user | in-thread replies (coarse) |
| Cursor Slack [18] | outside | new isolated virtual machine (VM) per invocation | task | pre/post |
| OpenTag [9] | inside (thread) | **reject** (lock + 409) | message | none while running |
| Parallel-agent tools [10] | outside (per-branch) | isolate-and-merge | branch | per-agent |
| Coagora, serial mode (shipped default) | **inside** | **serialize** (shared input queue; first-answer-wins gates) | turn (author labels) | during, session-scoped; steering *into* a running turn |
| **Coagora, parallel contract (this paper)** | **inside** | **parallel + serialized side effects** | **turn (`reply_to` 1:1)** | **during, turn-scoped; cancellation only** |

The two Coagora rows differ in more than the scope of intervention. Serial mode supports *steering*: a message injected at a turn boundary joins and redirects the active turn. The parallel contract currently supports only cancellation of a turn the user owns (§4.2); it cannot yet steer one specific in-flight turn (§7.6, item 8). Parallel mode therefore improves precision—you stop your own work and no one else's—but reduces expressiveness. The design space must show both sides of that trade-off.

The map reveals three patterns. First, messenger integrations form a tight cluster: state outside the conversation, task-level attribution, and intervention only before or after execution. The limits from §1 follow from those coordinates, not from one vendor's implementation. Second, no surveyed system combined state inside the session, parallel execution, turn-level attribution, and turn-scoped intervention. Third, the two Coagora modes share the same system and differ on the central concurrency axis, D2.

The remaining sections design, implement, and measure that move. The serial and reject conditions are not hypothetical baselines: deployed systems use both, and Coagora itself ships the serial one.

---

## 4 The Contract: Parallel Inference, Serialized Side Effects

### 4.1 Model

A **session** is a triple *S = (C, W, T)*. It contains one shared conversation context *C*, one working directory *W*, and a set of turns *T*. Any number of clients may attach. The agent generates each event once and broadcasts the ordered stream to every subscriber. A session always has one agent process; adding a user does not create another process, virtual machine, or context fork.

The contract is two commitments:

> **(P) Parallel inference.** Every user message becomes an independent turn *t_i*. Inference for *t_i* starts immediately, irrespective of other in-flight turns; responses stream concurrently to all participants.
>
> **(S) Serialized side effects.** Every effect any turn produces (file write, tool execution, and every commit to *C*) is applied through a per-workspace serial admission mechanism, in completion order, and never concurrently with a conflicting effect.

The two commitments are intentionally asymmetric. Inference is read-only, lasts seconds or minutes, and dominates what users wait for. Blocking it makes one person's task length part of another person's latency. Effects are shorter and are the only way turns can corrupt shared state. We therefore keep inference parallel and order only conflicting effects.

### 4.2 Turns: independence, multiplexing, and 1:1 attribution

Concurrent messages are not batched into one prompt. Batching produces a fused answer with no clear owner, removes per-message inference settings, and eliminates the opportunity to interrupt one turn. We treat attribution as a correctness property, not a display detail.

Each turn receives a monotonically increasing **turn id** when it is dispatched. Assigning the id at dispatch, rather than enqueue, ensures that every id represents work actually sent to the model. Streamed text, tool calls, acknowledgements, and errors all carry this id. The runtime uses it to separate concurrent output, persistent replies store a `reply_to` link to the originating human message, and the interface renders each stream as its own labeled card.

**Turn-scoped interrupts.** An interrupt names one turn id and cancels only that turn. The system preserves its partial output, marks the turn complete, and releases its slot. Other turns continue inferring, streaming, and applying effects. The registry checks the caller's connection id and rejects attempts to cancel another user's turn. The interface follows the same ownership rule: only a user's own active turn disables that user's composer. This is a minimal multi-party control policy—each person controls their own work—and §7.2 explains why richer policies remain necessary.

### 4.3 Shared context: snapshot read, completion-order atomic commit

All turns share the same conversation context *C*. Forking it per user would simply recreate isolate-and-merge. Two rules keep the shared context coherent:

- **Snapshot read.** At each inference step, a turn reads an immutable snapshot of *C* as its prompt. In-flight partial output of other turns is never visible.
- **Atomic completion-order commit.** When a step completes, the turn commits its results (the assistant message together with *all* corresponding tool-result messages) as one atomic block, through the serial commit chain, in completion order. The block boundary is an invariant: splitting an assistant/tool-results pair would let another turn's commit interleave between them, violating the message-pairing constraint that LLM chat APIs enforce (and rejecting the request outright).

Together, these rules apply snapshot isolation to a conversation (§2.4). The benefit and the limit are the same as in a database.

The cost is bounded staleness. A turn may not see effects committed after it took its snapshot. We measure this cost rather than claim to remove it (§6.7, Limitation 5). It affects freshness, but not structural consistency: *C* always remains a well-formed transcript that some serial execution could have produced.

The inherited boundary is write skew [32]. Two turns can read the same valid snapshot, modify different files, and jointly break an invariant. For example, both may see a function with one caller and independently decide that changing its signature is safe. Their writes are intact, ordered, and correctly attributed, yet the repository is inconsistent. Effect serialization cannot detect this error because the conflict lies in the turns' reasoning, not in overlapping writes. The contract therefore guarantees physical integrity, not semantic correctness; Limitation 3 returns to this distinction.

### 4.4 The effect layer: from a global mutex to a conflict-scoped hierarchical lock

Our first executor used one mutex for the entire workspace. It preserved correctness but also queued turns that touched different files. Section 6.4 measures when that matters. When effects occupy most of a turn, two disjoint-file turns achieve effective parallelism of 1.37 with the workspace mutex and 1.98 with conflict scoping, a 1.45× recovery. When effects are a small part of the turn, the scopes behave alike because there is little effect time to serialize.

The current executor therefore locks at the *conflict* level. It classifies each effect intent and checks a compatibility matrix. Following granular locking [33], each intent has a lock mode, compatible modes can coexist, and operations with an unknown resource set escalate to a coarser lock.

| Intent pair | Decision | Rationale |
|---|---|---|
| FILE_WRITE/READ(path *p*) vs FILE_WRITE/READ(path *q*), *p* ≠ *q* | **parallel** | disjoint resources |
| FILE_WRITE/READ(*p*) vs FILE_WRITE/READ(*p*) | serial | same resource |
| anything vs SHELL or PACKAGE | serial (exclusive) | a shell's file footprint is statically unknowable (pipes, expansions, subshells); there is no sound path key |
| anything vs FILE_DELETE | serial (exclusive) | deletion can remove directories; letting `rm -r src/` run beside `write src/x.py` *creates* an ENOENT race class that did not exist under exclusion; deletions are rare, so exclusivity is nearly free |
| not orderable (composite tools, human-wait tools, non-workspace state) | **no lock** | these have no workspace effect of their own; see below |
| unknown file effect | serial (exclusive) | safety-first default |

Path keys use normalized separators and, on case-insensitive filesystems, normalized case so that `A.txt` and `a.txt` conflict. Admission is **strict FIFO per workspace, with no overtaking**. If the first waiting intent conflicts with a running one, the executor stops instead of admitting a later compatible intent. This trades some concurrency for fairness: a stream of file operations cannot starve a waiting shell command. A runtime switch (`--lock-scope off | workspace | conflict`) provides a rollback path and the ablation arms used in §6.2 and §6.4.

The *not orderable* category avoids a deadlock created by the threading model. Composite tools, such as sub-agents and skills, start nested agent loops. Their leaf calls acquire the effect lock from another thread. If the parent held an exclusive lock while waiting for a child, neither could proceed; thread re-entrancy would not help because the threads differ.

Three kinds of tools belong in this category. Composite tools lock at their leaves. Human-wait tools may pause indefinitely and must not block every other effect. Tools such as session memory, the code index, and network fetches modify state outside the workspace and already have separate guards. None has a workspace effect for this lock to order. We therefore treat them as outside this lock's scope, while an unknown *file* effect remains exclusive by default.

This placement separates our contract from optimistic designs (§2.4). Locks do block inference when they sit in the inference path; our lock never does. Incompatible effects are ordered before they run, so there is no later conflict to detect, roll back, compensate, or merge. Writes to the same resource follow a last-writer outcome determined by their order. Both pessimistic ordering and this outcome are classical [34, 35]. Our contribution is to place them below inference and measure the resulting trade-off (§6.4).

### 4.5 Admission: cap, fair queue, one turn per user

Parallelism needs a limit because every extra turn consumes tokens. A session therefore caps in-flight turns at four by default. Requests beyond the cap enter a fair queue; they are neither rejected nor batched. The queue scans from the front for the first eligible request. A user is eligible only when they have no active turn, so parallelism occurs between users rather than within one user's backlog. One person cannot occupy every slot by submitting many requests. System-initiated turns are exempt, and a queued request from one user never delays another user's immediately eligible request. Section 6.1 shows why rejecting excess work and asking clients to retry performs worse.

### 4.6 Session state and lifecycle

Session state depends on the number of active turns. It is RUNNING when at least one turn is in flight and IDLE only when the count reaches zero. No single turn defines the session's state, so one turn finishing cannot cause an idle-state flicker while others continue. An idle session may suspend: its process stops, while its directory and history remain available for a later resume.

The concurrency contract is chosen when the session starts and cannot change during that process. Serial and parallel turns therefore never coexist under different context rules. An unknown contract name causes a startup error instead of silently falling back to serial, because such a fallback could invalidate a full experiment.

### 4.7 Ordering, replay, and idempotence

A shared session must preserve order even when clients disconnect. Every persistent event receives a monotonically increasing `seq` number when it enters the replay buffer. The same lock both allocates the number and appends the event. If numbering happened earlier during fan-out, two turns could receive numbers in one order and enter the buffer in another. A reconnecting client would then replay an order that no live viewer saw.

The server sends `seq` as the Server-Sent Events (SSE) `id`. On automatic reconnect, browsers return it as `Last-Event-ID`, allowing the server to send only missed events. Three cases require a reset and full snapshot: the cursor is older than the retained buffer, it is ahead of every issued id, or it came from a previous process. A per-process stream epoch distinguishes the last case and prevents a resumed process from silently joining a new stream to an old one. Only persistent, replayable events carry `seq`; transient token deltas and spinners do not. Section 6.5 tests whether a repeatedly disconnected client ends with exactly the same transcript as a continuously connected client.

---

## 5 Implementation

Coagora contains about 38,400 lines of Python and 6,300 lines of dependency-free browser assets. It supports both a single-user terminal and a multi-user LAN web interface. Both front ends call the same agent loop through a renderer interface, so multiplayer support does not require a separate execution path. The public repository contains the implementation and raw data. This section focuses on the decisions needed to reproduce the concurrent session.

**One process, many threads.** Each session is one operating-system process, and turns run as threads over a shared heap. They are not child processes with a parent that can broker every effect. The guarantees in §4 must therefore be enforced inside one address space.

**Lock discipline in the context manager.** Every turn reads snapshots from the shared context and commits results to it; compaction also modifies it (§6.6). To avoid a session-wide deadlock, locks are always acquired in one direction: context → renderer → file append. Each acquisition site documents this order, and no code may acquire the same locks in reverse.

**Turn scoping belongs to each prompt.** Every parallel turn runs its own agent loop and builds its own system prompt. With `--turn-scoping`, that prompt names the request the turn should serve (§6.7). The section is attached after every prompt rebuild, because editing project directives through the web inspector rebuilds the prompt from scratch. Scoping is active only when both the flag and the parallel contract are enabled. Under serialization, an injected question is supposed to redirect the running turn, so the same instruction would be wrong. The measurements in §6.7 made scoping the default for parallel sessions; `--no-turn-scoping` remains available for ablation.

**Attribution must be thread-local.** Each thread stores the id of the question it serves, and every record it writes carries that id in `reply_to`, regardless of the model's output. Our first implementation used one session-global field. That field appeared correct in serial mode, but under concurrent threads it attributed all three replies to the latest question. A live run exposed the bug. We moved the field to thread-local storage and added a regression test; §6.7 verifies the resulting attribution.

**The production seam must use the atomic commit.** The context manager already had the completion-order atomic commit from §4.3, together with concurrency tests. However, instrumentation for the staleness study (§6.7) revealed that the production step path did not call it. That path appended an assistant record and its tool observation separately. Serial execution made this look correct, but parallel execution allowed another turn to split the pair. The step path now commits both records through the atomic primitive, and a regression test verifies that concurrent appends never divide them.

We then repeated every mock experiment whose result could depend on history order: compaction (§6.6), attribution and scoping (§6.7), fairness (§6.8), and lifecycle (§6.9). The repaired path reproduced every structural result: the mint chain and `reply_to` mapping remained perfect, all 90 and 204 queries were preserved in the relevant studies, all four compactions committed, and no admission gate failed. Scheduling-dependent content rates changed between repetitions, including one unscoped run at 38%, which matches the variability discussed in §6.7. No committed headline number changed; the new raw data appears in `out/postfix/`.

We also repeated the live experiments at their original sizes. Their conclusions held. Turn scoping again reduced contamination to 0 of 40, compared with 25 of 40 without it (the earlier run was 0 versus 26). The §6.4 operating point remained an effect share of 10⁻⁵ and a 0.1 ms lock wait. The §6.10 latency and token ratios reproduced at 3.55× and 1.486×, while absolute times varied with endpoint load.

One result did change: contamination in §6.4's disjoint condition rose from 1 of 6 runs to 6 of 6. The dedicated §6.7 study stayed nearly constant at 25 versus 26 of 40, so record ordering does not explain the change; endpoint load and the resulting overlap do. Together, the two bugs show why concurrent systems must test the production seam, not only primitives that appear correct under serial execution.

**Append atomicity is filesystem-dependent.** With no parent process serializing writes, the durable history is appended to directly by every turn. A probe found that on local ext4 this is safe (one append is one `write(2)` call, so `O_APPEND` atomicity holds, and 16 threads appending 8 times each survive intact), but on Windows Subsystem for Linux (WSL) mounts of Windows drives the same probe loses most of the data: at a 4 KiB payload only 45 of 128 lines survived and 15 of those were malformed JSON, and at 1 MiB only 28 of 128. The failure mode is the dangerous kind, because the record loader skips unparseable lines silently, so a resumed session would simply be missing history with no error.

The fix is a striped in-process append lock (a fixed number of stripes rather than one lock per path, so a long session cannot accumulate locks without bound); under the lock the same probe returns 128 of 128 with zero corruption. Concurrent appends to one shared history file are created by multi-user parallel turns and by nothing else, which is why this hazard appeared exactly when the contract did.

**Tool calls are parsed from streamed content.** The agent does not depend on provider-native function calling; it parses a JSON operation array out of the model's content stream, which keeps the system portable across on-premise servers whose function-calling support varies. The parser is incremental, so a turn's first tool call can be dispatched before the response finishes streaming.

**The web surface.** The multi-user surface is HTTP: a single SSE stream per client downstream, plain POST upstream. SSE is a deliberate choice over WebSocket here, because browser-native automatic reconnection with `Last-Event-ID` makes the replay of §4.7 a property of the transport rather than application machinery, and because the upstream direction carries only occasional small messages. Authentication is a shared session token compared in constant time. A second, read-only token may be enabled (off by default), and a client presenting it may subscribe to the stream and nothing else: every mutating endpoint answers 403, as do read endpoints that would expose more than the transcript (prompt inspection, project directives, workspace contents). The split exists because "let people watch" and "let people act" are different grants, and a demonstration or a review is the former.

**Suspend and resume.** A session's durable state is an append-only history file. On resume the renderer replays that history into the event buffer before any client attaches, so a reconnecting browser sees the prior conversation through the ordinary snapshot path rather than a special case. Identifier counters are re-derived from the *full* history rather than from the compacted cache, so a resumed session never re-mints an id that compaction has already dropped from the working window but that remains attributed in the record (§6.9).

**A measurement plane, opt-in.** With `--turn-metrics`, the session appends structured events to `turns.jsonl`. These events cover the turn lifecycle, effect-lock wait and hold times, compaction generations, rejects, and per-call token use. All use one monotonic clock. Every number in §6 comes from this file or the durable history, not from client timing, so no clock-skew correction is needed.

Time-to-first-token has one definition across all three contracts: the interval from the earliest server record of a user's first attempt to that user's first token. An accepted request creates an enqueue record; a rejected request creates a reject record. The reject condition therefore starts at the first 409 and includes the client's complete retry wait. The measurement plane stores structural metadata only, never prompt or response text.

**Determinism for science.** The benchmark harness (§6, `bench/multiuser/`) drives the real server over HTTP against a local mock LLM: an OpenAI-compatible SSE server whose latencies, token counts, and tool steps are scripted by a directive embedded in the prompt (`[[bench ttft= tok= n= work= fwrite= …]]`; Appendix A lists which script drives which experiment), with no random or clock-dependent branching. Identical inputs produce identical timelines, the suite runs without network or credentials, and each condition gets a fresh temporary workspace with an isolated `HOME` so a benchmark can neither read nor write the operator's configuration. Two experiments deliberately bypass the model layer and drive the tools and the lock directly (§6.2, §6.4). The reason is a measured property of the harness, not an assumption.

The mock reads progress out of the conversation (observations following the most recent directive), and in a *shared* session the concurrent turns' observations land in one counter. Logging which directive each call resolved shows the consequence precisely: each turn's first call picks its own directive, but continuation calls collapse onto the newest one, after which a turn writes to the other turn's path and trips the agent's own repetition detector. There is no per-turn signal in a shared prompt for the mock to key on, so this is not a bug to fix but a limit of scripting two different concurrent workloads through one shared context.

Experiments where concurrent turns may run the *same* workload (§6.1, §6.6 through §6.9) are unaffected and use the mock. Where the real operating point is the question rather than a controlled sweep (§6.4), we use the live model, which reads its own turn's request and does not have this limit.

---

## 6 Evaluation

The evaluation asks whether the contract works, where it stops helping, and what it costs.

| Question | What we test |
|---|---|
| RQ1–RQ2 | Whether parallel turns remove head-of-line blocking, and what serial and reject contracts cost |
| RQ3 | Whether effect serialization prevents physical corruption |
| RQ4 | Where parallelism collapses, what finer locks recover, and where real workloads fall on that curve |
| RQ5–RQ6 | Whether clients can reconnect exactly and the shared context can compact while turns continue |
| RQ7–RQ9 | Whether attribution, fairness, and lifecycle remain correct under concurrency |
| RQ10 | Whether the ranking holds with a live LLM and how many additional tokens parallelism consumes |
| RQ11 | What first-time users notice, how they explain events, what coordination norms they form, and where they would use the system |

**Scope of the evaluation.** This is a systems evaluation of a working artifact. It compares the contract with the two alternatives that deployed systems use, all within Coagora's production path. In Ledo et al.'s taxonomy for HCI systems and toolkits [41], the method combines technical benchmarking with demonstration. It does not test whether teams collaborate better. Section 7 turns grounding, arbitration, and accountability into a human-subject research agenda, but this section evaluates only implementation guarantees and costs.

**Setup.** Headline experiments use the deterministic harness from §5. The server, workspaces, locks, and transport are real; only LLM timing and behavior are scripted. Runs are reproducible without network access or credentials. Every comparison uses the same binary and changes one switch (`--concurrency-contract`, `--lock-scope`, `--per-user-gate`, or `--turn-scoping`). Section 6.10 repeats the central comparison with a live on-premise model. Raw JSONL for every figure is committed under `bench/multiuser/out/`. ✔

Where a contrast between arms is quoted below, we report a two-sided Fisher's exact test for counts and a fixed-seed percentile-bootstrap 95% confidence interval (10,000 resamples) for medians, recomputed from the committed raw files (`stats_recompute.py`); experiments whose committed artifacts are aggregates rather than per-run samples (§6.8) report aggregates only.

**A note on the fixed overhead.** Every turn carries a constant setup cost (agent-loop assembly plus the scripted first-token delay). It is identical across contracts, so it cancels in slopes and in same-condition comparisons, but it does drift slightly between measurement sessions. The head-of-line grid below was collected in two sessions (L ∈ {2, 6, 15} s and, later, L = 30 s), and the constant fell by about 56 ms between them. A control re-measurement of L = 15 s in the second session reproduces the second session's constant exactly, confirming the shift is environmental rather than an effect of *L* (`out/e2-drift-control.json` ✔).

The consequences are bounded and stated where they apply: slopes are immune, because a constant offset cancels in a regression; contract-to-contract comparisons at one *L* are immune, because each session measured all three contracts of that level together; only cross-level absolute comparisons cross the boundary.

### 6.1 RQ1 and RQ2: Head-of-line blocking under three contracts ✔

User A begins a task lasting *L* ∈ {2, 6, 15, 30} seconds. User B asks a one-line question 0.5 seconds later. We measure B's time-to-first-token from the first server-side record of B's attempt. For the reject arm, that record is the first 409, so retry time is included. We compare Coagora's shipped FIFO contract, reject-and-retry with a 250 ms interval, and the parallel contract. Of 240 runs (20 per cell), 236 produced an attributable first token and enter the analysis; we account for the remaining four below. A's task contains only token streaming and no tool boundary, an important condition discussed later in this section.

| Contract | L = 2 s | L = 6 s | L = 15 s | L = 30 s | slope dTTFT/dL |
|---|---|---|---|---|---|
| Serial (FIFO, shipped mode) | 2.08 s | 6.16 s | 15.34 s | 30.86 s† | 1.028 |
| Reject + retry | 2.31 s | 6.34 s | 15.42 s | 31.05 s† | **1.027** |
| **Parallel (ours)** | **0.292 s** | **0.291 s** | **0.291 s** | **0.235 s†** | **-0.002** |

† Measured in a second session whose fixed overhead is about 56 ms lower (see Setup). Slopes and same-*L* contract comparisons are unaffected; only cross-level absolute comparisons cross this boundary, which is visible in the parallel row because its latency *is* that constant and invisible in the other two where *L* dominates. Numbers are reported to the precision the raw data supports: slopes to three decimals, latencies to three significant figures.

The central result is the slope. Under serial and reject contracts, it is about 1.03: every extra second in A's task adds about one second to B's wait. The measured latency ratios grow from 7.1× at *L* = 2 seconds to 131× at *L* = 30 seconds, but those ratios merely reflect where the experiment stops. Under the parallel contract, the slope is approximately zero. B's latency depends on B's request rather than A's task length.

**Fit, dispersion, and the four unanalysed runs.** The slopes above are least-squares fits over cell medians; refitting over all 236 individual points moves nothing material (serial 1.028 either way, reject 1.027 versus 1.026, parallel −0.002 either way), and the serial and reject relationships are almost perfectly linear (R² = 0.99998 and 0.99992). We deliberately do not report R² for the parallel row: goodness of fit against a variable the response does not depend on is not meaningful, and the parallel row's entire variance is the 56 ms session drift described in Setup, so its R² of 0.81 is a description of that step rather than of any dependence on *L*.

Dispersion separates the contracts as cleanly as slope does. Parallel's p95 − p50 is 1.7 ms to 2.7 ms across the four cells with a standard deviation never above 1.7 ms; serial's spread grows with *L* (p95 − p50 of 9.8, 31.7, 69.8 and 106.0 ms, standard deviation 8.0 to 69.4 ms), which is what queueing behind a longer stream does. Reject is the least predictable of the three, because its retry phase is synchronized with nothing: at L = 15 s its p95 sits 268 ms above its p50 with a standard deviation of 99.6 ms, an order of magnitude above serial's at the same level.

Finally, 4 of the 240 runs (2 parallel at L = 2 s and 6 s, 2 reject at L = 2 s and 15 s) recorded no first token attributable to B and are excluded from every median and fit above. We report them rather than silently dropping them; we did not diagnose them, and at 1.7% of runs spread across two contracts and three levels they cannot account for a slope difference of 1.03 against 0.00.

Three readings deserve care. First, the parallel row is flat at 0.291 s for the three levels of one measurement session and at 0.235 s for the level measured in the other; the pooled slope is -0.002 and the within-session slope is -0.00003, and both say the same thing. The visible step is the fixed-overhead drift described in Setup, and it is itself a small demonstration of the claim: the parallel contract's TTFT is *nothing but* that constant, so a change in the constant is fully visible there while it is invisible in the serial and reject rows (15.337 s → 15.360 s at the same level, a 0.15% shift) where *L* dominates.

Second, the reject contract deserves emphasis because it is what deployed multi-user frameworks actually ship. It pays serial's entire head-of-line wait *plus* a **phase penalty**, measured here at +232 ms, +182 ms, +79 ms, and +190 ms across the four levels against a 250 ms retry interval: a phase-dependent fraction of one interval, bounded below by zero when a retry happens to land just as the running turn completes and above by one full interval.

It is therefore *never better than serial*, and it additionally pushes retry machinery onto every client. Its slope matches serial's because the penalty is a constant, not a function of *L*: rejection does not make the wait grow faster, it makes the same wait slightly longer and the client more complicated.

That penalty is measured against a retry interval we chose, so we checked whether the conclusion depends on the choice ✔. Holding L = 15 s and measuring serial in the same session as the baseline, a 250 ms interval cost 172 ms over serial and a 1,000 ms interval cost 794 ms, which is 0.69 and 0.79 of one interval respectively, with the client spending 61 and 16 requests to get in. The penalty scales with the interval and stays within one interval of it, exactly as a phase-dependent fraction should, and the ranking is unchanged at both settings.

A client that retries gently to spare the server is simply served later; one that retries hard converges on serial's latency while spending more requests to reach it. Reject is never better than serial at any interval, and the interval controls only how much worse it is.

Third, and this is the strongest form of the objection to the comparison, *what exactly is the serial arm?* Serialization here is not a bare queue. At every internal turn boundary of a running agent loop, the serial contract dequeues one waiting user message and folds it into the turn in progress; §6.10 measures that behaviour from the other side, where serial answers three questions in two model calls. If that injection can fire, B's answer may be produced *inside* A's run, and serial's time-to-first-token is bounded not by *L* but by the time remaining to A's next boundary.

So the grid above must be read for what it holds fixed. A's task is a single streaming generation with no tool step, so A's loop has exactly one turn and offers exactly one boundary, its end. Injection is enabled in the serial arm and never fires, not because we disabled it for the measurement but because this workload gives it no opportunity.

That was a deliberate choice, and it cuts both ways. It isolates the mechanism: head-of-line delay under serialization comes from the worker being occupied for *L* whatever occupies it, and pure inference removes mid-run injection and effect-lock contention as confounds, so the slope measures the contract rather than the workload's tool structure. But it also measures serial at its most exposed, and the honest statement of the finding names the axis being held fixed.

Serial's real head-of-line exposure is governed by **boundary density**: a loop that pauses for a tool every few seconds gives injection frequent opportunities and can serve B well before A finishes, whereas a loop emitting one long generation, or blocked in one long build, gives it none. The regimes that dominate real coding work, a multi-minute test run or a large single-shot edit, are exactly the low-density ones, which is why we take *L* rather than boundary count as the headline axis. But we have not yet measured the boundary-density axis, so the claim this table supports is narrower than the ratios suggest: *under serialization a second user waits for the first user's work to reach a boundary, and this grid measures the case where the only boundary is the end.* The next experiment measures the axis this one holds fixed.

**The boundary-density axis, measured ✔.** Holding total task length fixed at L = 15 s, we varied the number of model calls in A's task, k ∈ {1, 2, 4, 8}, by splitting it with k − 1 tool steps, and measured when B's question reached the model. Ten repetitions per cell, serial contract throughout. The metric is **time-to-inclusion**: from B's submission to the moment B's question entered a model call's prompt. It is deliberately not B's time-to-first-token, for an instrumentation reason that also bounds what serial can be said to deliver: the first-token event is latched once per run, so a question folded into a running turn has no first token of its own to attribute. Inclusion is therefore generous to serial twice over, since it stops when B's question entered a prompt rather than when B received an answer, and since under a scripted model the call that follows continues A's work rather than answering B. We report it because even this generous lower bound settles the question.

| k (model calls in A's task) | boundary interval L/k | injection fired | time-to-inclusion p50 |
|---|---|---|---|
| 1 | 15.0 s | 0 of 10 | 15.10 s |
| 2 | 7.5 s | 10 of 10 | 7.30 s |
| 4 | 3.75 s | 10 of 10 | 3.39 s |
| 8 | 1.875 s | 10 of 10 | 1.44 s |

Three readings. First, the consistency check holds. At k = 1 injection never fired (0 of 10), exactly as the disclosure above predicts, and B's time-to-first-token, measurable there because B receives its own run, was 15.41 s against the 15.34 s the headline grid reports for the same level: 0.5% agreement across two measurement sessions. The grid's serial cell *is* the k = 1 cell.

Second, the law is exact. Regressing inclusion on the boundary interval across all 40 runs gives a slope of 1.041 with R² = 0.99995 and an intercept of −514 ms, and that intercept is not noise: it is B's 500 ms submission delay, which is precisely what a mechanism that makes B wait from its own submission until the next boundary predicts. The 4% slope excess is per-call streaming overhead, which grows with the tokens each call emits. Third, the magnitude. At an identical *L*, moving from one boundary to eight cuts B's wait 10.5-fold, from 15.10 s to 1.44 s.

The conclusion is the one the headline grid could not reach alone. *Serialization's head-of-line cost is set by the boundary interval, not by task length.* The grid measures *L* because it fixes k = 1, and k = 1 is not a strawman configuration but the shape long work actually takes: a single long generation, or a single long build, offers the queue nothing to interleave with. Where boundaries are dense, serial degrades gracefully toward one boundary interval; where they are sparse, it approaches the whole task. What it never does is what the parallel contract does. Even at k = 8, and even measuring serial by a lower bound that stops short of an answer, B's 1.44 s is 4.9× the parallel contract's 0.29 s at the same level, and it remains a function of A's work rather than B's.

**The user-count axis, measured ✔.** The headline grid fixes a second thing: it has exactly two users. With A running the same L = 15 s task, we had N − 1 questioners submit simultaneously 0.5 s later, for N ∈ {2, 4, 8} total users, with the concurrency cap set to N so that the contract rather than the admission gate is what is under test (the gate has its own experiment, §6.8). Ten repetitions per cell, 110 questioner turns in all. Median time-to-first-token was 236 ms at N = 2, 265 ms at N = 4 and 328 ms at N = 8, with p95 within 33 ms of the median in every cell. Seven simultaneous questioners therefore cost each of them 39% more than a single questioner does, not 7×. That growth is real and we do not hide it: it is the runtime's cost of carrying more concurrent turns, in threads, connections and fan-out of one output stream to more subscribers, and it is a cost of concurrency itself rather than a wait on another user's work.

What matters for RQ1 is that the headline row's flatness is not an artifact of having exactly two users. At four times the user count a questioner's latency is still its own request plus a bounded runtime overhead, and still 47× below the serial contract's two-user figure at the same task length, a comparison that flatters serial, since serial's questioners would also have to queue behind one another.

### 6.2 RQ3: What effect serialization prevents ✔

We force two writers to update the same file repeatedly. Their payloads have distinct markers and checksums. A 2 ms sampler classifies each snapshot as intact, mixed (a torn write containing both markers), broken (invalid structure or checksum), or partial (a write currently in progress). We compare the lock disabled with both available lock scopes.

Without the lock, **8.2%** of classified snapshots were violations: 9 of 110, all torn writes combining both writers' content. With either lock scope, the rate was **0%**: 0 of 181 under workspace scope and 0 of 180 under conflict scope (two-sided Fisher's exact test, 9/110 versus 0/361 pooled locked snapshots: p = 1.6 × 10⁻⁶). The lock prevents same-file writes from overlapping, so no merge, conflict dialog, or rollback is needed.

**Why this arm cannot use a live model.** The ablation drives the two writers and the lock directly, and here that is not a convenience but the only way to ask the question. A violation can only occur while two writes physically overlap, so measuring a violation *rate* requires overlapping writes at a rate high enough to sample. §6.4's live-model measurement shows why that is unreachable through the agent loop: a real turn produces about eight sub-millisecond writes scattered across four minutes, an effect time share of 10⁻⁵, and even when both turns target the *same* file the measured lock wait is 0.1 ms, which is to say their writes essentially never coincide. Reproducing the 110 classified concurrent-write snapshots of this experiment through a live model would take on the order of thousands of turns and would still yield a rate dominated by how rarely the writes happened to collide, not by what happens when they do.

Forcing the overlap and asking whether the lock holds is the standard way to measure an I/O race, and it separates the two questions cleanly: §6.4 measures *how often* concurrent effects collide in real work (rarely), and §6.2 measures *what happens when they do* (corruption without the lock, none with it). The claim this experiment supports is conditional by construction, and the condition is what the other experiment quantifies.

The mechanism is worth naming because it bounds the claim. A write here is a truncate followed by one large `write(2)`, which leaves a narrow interleave window, and the violation rate is a property of that window: a platform whose executor streams writes in chunks would widen it, and one with fully atomic replacement would close it. What is invariant is the direction and the zero. We therefore report the rate as evidence that the hazard is real on this platform, and the zero as the contract's guarantee, rather than presenting 8.2% as a universal constant.

### 6.3 RQ4a: Where the parallel benefit collapses ✔

Parallelism does not disappear merely because turns write files. Two turns with no file work reach effective parallelism of 1.99. Adding ordinary file writes keeps median lock wait below 1 ms in every cell, regardless of lock scope or whether paths match. Real file writes last microseconds to milliseconds and rarely overlap. The boundary depends on the *share of time* spent inside exclusive effects, not on whether a turn writes at all.

We flag a limitation of that grid rather than over-reading it. Its intended second axis, the *count* of writes per turn (0, 2, 6), did not in fact vary the work: turn duration is identical at 2 and 6 writes (2,897 ms and 2,898 ms median), because the mock model resolves whichever `[[bench …]]` directive is newest in the *shared* transcript, so concurrent turns collapse onto one script. That is the same trap that makes §6.2 and §6.4 bypass the model layer entirely, and it means only the lock-wait result above should be read from this grid.

What *does* collapse the benefit is the **time share of exclusive effects**, and two independent measurements agree on it. With each turn spending about 50% of its span inside an exclusive shell step, effective parallelism (lock wait excluded from useful work) falls to 1.59; at about 90% it reaches **1.10**. The controlled experiment of §6.4, which drives the lock directly and calibrates the effect share on the host, reproduces the same law from the other direction: under a global lock, effective parallelism tracks the analytic ceiling for two turns, 2 / max(1, 2s) at effect share *s*, to within 1.3% at every share we measured (measured *s* of 0.23, 0.45, 0.60, 0.73 predicting 2.00, 2.00, 1.68, 1.38 against measured 1.99, 1.97, 1.66, 1.37).

The design-space statement: *the collapse boundary tracks the exclusive-effect time share, not the effect count or the conflict rate.* Since inference dominates real coding-agent turns while their file effects are brief, the common case sits far from the boundary, and the lock's remaining job in effect-heavy regions is integrity (§6.2), not latency.

### 6.4 RQ4b: What conflict-scoped locking recovers, and where real work sits ✔

§4.4 claims a global mutex is too blunt because it serializes turns touching disjoint paths. Establishing that claim needs two measurements, because neither answers the question alone. A **controlled sweep** establishes how the cost of a coarse lock varies with the effect time share. A **live-model run** establishes where on that curve real work actually sits. The first without the second gives a law with no located operating point; the second without the first cannot tell "the lock scope does not matter" from "this workload happens to sit where it does not matter yet."

**The controlled sweep.** Two turns alternate an unlocked think interval with a locked write, under each lock scope, with the write directed either at *disjoint* paths or at the *same* path. The think interval is set from a run-time calibration of how long one write actually takes on the host (26.39 ms for 1 MiB in this run), so the effect time share is a controlled axis rather than a hoped-for property. This arm drives the tools and the lock directly, for a reason given in §5 that we measured rather than assumed. Five repetitions per cell, 120 runs.

| Effect time share | Paths | `workspace` (global) | `conflict` (ours) | recovery | `off` (reference) |
|---|---|---|---|---|---|
| 25% | disjoint | 1.987 | 1.992 | 1.003× | 1.994 |
| 25% | same | 1.988 | 1.988 | 1.000× | 1.994 |
| 50% | disjoint | 1.974 | 1.983 | 1.005× | 1.986 |
| 50% | same | 1.978 | 1.976 | 0.999× | 1.988 |
| 75% | disjoint | 1.662 | 1.977 | **1.19×** | 1.988 |
| 75% | same | 1.656 | 1.653 | 0.998× | 1.987 |
| 90% | disjoint | 1.368 | 1.983 | **1.45×** | 1.992 |
| 90% | same | 1.369 | 1.368 | 0.999× | 1.991 |

Read the *same*-path rows first: they are the control, and they show the two scopes are indistinguishable when the paths genuinely conflict (1.000×, 0.999×, 0.998×, 0.999×). The gain on disjoint paths is therefore not bought by weakening the guarantee; it is the cost of serializing work that never needed ordering, returned. Median lock wait under the global mutex tells the same story across the sweep: 28.9 ms, 26.6 ms, 553 ms, 1,077 ms. The `off` column bounds the remaining overhead: conflict scoping runs within 1% of no lock at all, while §6.2 shows what `off` costs in integrity.

The two low rows are not null results to be explained away; they are the shape of the effect. With two turns, the ceiling on parallelism when effects are fully serialized is 2 / max(1, 2s) for effect share *s*, so at *s* ≤ 0.5 a global mutex can be perfectly blunt at no cost, because one turn's think interval hides entirely behind the other's effect. The `workspace` column follows that ceiling to within 1.3% at every share measured (§6.3). The cost of a coarse lock is thus not a constant overhead to be tuned away: it is zero below the line and grows with the effect share above it.

**Where real work sits.** The sweep's lowest measured share is 0.23. To find the real operating point we ran the same comparison with no mock and no bypass: the live on-premise model driving real turns, two users each instructed to write four files, under each lock scope and each path condition, 12 runs. Effect share is derived from the server's own lock instrumentation (hold time attributed to turns by thread) rather than from the harness.

The measured operating point is **an effect time share of 1 × 10⁻⁵**, identical at the median and the maximum across all 12 runs: 3.4 ms of lock hold against turn spans of 263 s. Lock wait was 0.1 ms at both median and maximum.

There is no scope difference to speak of (disjoint: 1.864 workspace against 1.854 conflict; same: 1.873 against 1.890), though the first of those four figures rests on 2 valid runs rather than 3, one disjoint/workspace run having been excluded by the cross-task filter described below, and the same-path result is the more striking one: two turns writing the *same* four files never contend measurably, because eight sub-millisecond writes scattered across four minutes essentially never overlap.

**Re-measured on the repaired seam ✔.** We repeated this arm at the same size after the step-seam fix of §5. The two numbers this section rests on reproduce exactly: the effect time share is 1 × 10⁻⁵ and the median lock wait 0.1 ms in all four cells, as before, with turn spans about 20% longer because the endpoint was busier (296 s against 246 s in the same-path cells). The same-path parallelism figures also reproduce (1.902 and 1.925 against 1.873 and 1.890).

What did *not* survive is the disjoint-path comparison, and the reason is the subject of §6.7: every one of the six disjoint runs had a turn carry out its neighbour's task, against one of six in the original, so no run implemented the intended path condition and the axis has no valid cells to report. We therefore keep the original disjoint figures, which are committed data, and record that a re-run could not reproduce the *conditions* for measuring them. This is the same phenomenon §6.7 measures directly, seen here as an obstacle rather than a result, and it is why the paragraph below now understates the rate by a wide margin.

**The mitigation restores the axis ✔.** After turn scoping became the default (§5, §6.7), we ran the disjoint condition a third time, with three repetitions per lock scope. This time we judged each turn by joining its `reply_to` value to the files it wrote. Final workspace contents are insufficient because the dominant failure mode completes both tasks and leaves a file union that looks correct.

All twelve turns wrote only their own four files. The intended disjoint condition held in 6 of 6 scoped runs, compared with 0 of 6 unscoped runs on the same code. The operating point also reproduced: effect share 10⁻⁵ at both median and maximum, lock wait 0.1 ms, and effective parallelism of 1.88 versus 1.84 across the two scopes. The parallelism identity held within 0.0003. Each scope now has three valid runs, replacing the earlier two-run cell.

Turn spans increased from the original 198–413 seconds to 354–703 seconds because the endpoint was busier, but the operating point did not move. In §6.7, contamination is a measured error rate; here, it determines whether the path axis can be measured at all.

That "no difference" is a measurement rather than a hedge, and it can be made exact. With lock wait at zero, makespan equals the longer span, so the metric reduces identically to 1 + shorter/longer. Across all 12 runs the measured value matches that identity to within **0.0004**. The parallelism the real system loses is entirely turn-length asymmetry, which is large with a live model (spans ranged 198 s to 413 s); the lock's contribution lies below that resolution.

**A second operating point: shell-dominated work ✔.** The file-write measurement invites an obvious objection, and it is the right one to raise. A coding agent's dominant effect is not writing files but running shells, builds and test suites, and §4.4 makes shell *exclusive* because its file footprint cannot be known statically. If shell occupies a large share of a turn, the operating point should move toward the knee. We measured it, same instrumentation, same two-user live configuration, with each turn running three shell commands instead of writing files. At one second per command the effect share is 0.025 and lock wait is 0 ms; at five seconds it is 0.094 and lock wait becomes 4.1 s.

Shell effects are three to four orders of magnitude heavier than file writes, so shell is the effect that matters. Its exclusive lock also creates measurable wait, exactly as §4.4 predicts. Even so, an effect share of 0.094 remains about five times below the 50% knee. The theoretical ceiling is still 2.00, while measured effective parallelism is 1.84 and 1.87; turn-length asymmetry explains the remaining gap.

The repaired-seam run reproduced this position. Effect shares were 0.021 and 0.085, compared with the original 0.025 and 0.094. Lock waits were 0 ms and 4.9 seconds, compared with 0 ms and 4.1 seconds, and effective parallelism was 1.94 and 1.82. The operating point remained stable under different endpoint loads, supporting the interpretation that it belongs to the workload rather than one session.

**What the pair establishes.** The law says the cost of a coarse lock is zero below a 50% effect share; the live system sits four orders of magnitude below that at 10⁻⁵ for file writes, and about one order below it at 0.094 for shell-dominated turns. The conclusion is therefore not "lock scope does not matter" but the sharper "this workload sits far below the knee, and here is the knee." It also explains why the live run cannot replace the controlled one: with turn-length variance of ±100 s swamping a 0.1 ms lock wait, the live measurement can bound the lock's contribution but cannot resolve differences within it. One experiment locates; the other resolves.

One observation recorded here as incidental turned out to be the section's most consequential, and we have left it in place rather than quietly promoting it. In 1 of the 12 live runs a turn carried out the *other user's* task, writing that user's files instead of its own. A re-run of the same configuration put that at 6 of 6 disjoint runs, and the dedicated experiment of §6.7 puts it at 25 to 31 of 40 turns depending on the workload; what we filed as a rare event is the common case.

This is the shared-transcript semantic confusion of §6.7 appearing not in an answer's text but in its **side effects**, and appearing with a real model rather than a deliberately adversarial one. The rate is not a general estimate: the two concurrent instructions here differ only in a tag and a filename, which we assumed at the time was close to the worst case for confusion, an assumption §6.7 later measured and found wrong. What it establishes is existence, and it is why the path-axis comparison above counts only runs that actually implemented the intended path condition (effect share and lock wait use all runs, since the writes happened and passed through the same lock either way).

### 6.5 RQ5: Does a reconnecting client end up with the same session? ✔

People close laptops, change networks, and join sessions late. A useful shared session must therefore survive disconnection. We test the `seq` and `Last-Event-ID` design from §4.7 by comparing two subscriptions to the same live session.

Three users drove 90 turns under the parallel contract. One subscription (*control*) stayed connected throughout. A second (*cutter*) disconnected and reconnected 11 times, each time presenting its last received id exactly as a browser's `EventSource` would, with cuts triggered by event count rather than by a timer so that every cut lands inside the traffic rather than after it.

The two subscriptions received **identical** streams: 180 replayable events each, equal in sequence number, event name, **and payload body**, with 0 missing, 0 duplicated, 0 out of order, and 0 payload mismatches. All 90 submitted question markers reached both. Because the comparison includes payload bodies, turn attribution is inside the result rather than beside it: a replayed event carrying a mangled or missing `reply_to` would be a mismatch. No reset was signalled during any of the 11 normal reconnections, which is the point of incremental replay: the client's transcript was continued, not rebuilt.

We also forced the fallback path with cursors the server could not honor. A cursor from another stream epoch and a cursor ahead of every issued id both produced the same response: a reset signal immediately after the connection identity, followed by a full snapshot of all 180 events.

The remaining case is a cursor older than the retained replay window. The shipped buffer keeps 5,000 persistent events. Because this experiment produced two replayable events per turn, reaching the boundary would require about 2,500 turns while one client remained disconnected, or roughly 1,300 tool-heavy turns. We cover this branch with a unit test. The limit affects only reconnection replay: connected clients still receive every live event, and the full transcript remains on disk.

### 6.6 RQ6: Compacting the shared context while turns run ✔

A shared context grows roughly N times faster with N active users, so it must be compacted. The difficult part is allowing turns to continue appending while the summary is generated (§2.4). Coagora uses an optimistic procedure: it splits the history and records a generation under the lock, releases the lock for the slow summary call, then rechecks the generation and commits the summary atomically together with any new turns.

Measured under forced pressure (3 users × 30 rounds, 90 turns, against a deliberately small advertised context window and an 800 ms summarizer): all four compactions began with 3 turns in flight, and turns kept flowing *inside* the no-lock summarize windows. Across those windows 42 turn events landed, including 11 first tokens, which is the availability a barrier design would set to zero by construction. Zero of 90 queries were lost, and every one of the 90 final records resolved to a real question through `reply_to`, so structural attribution survived every pass. All 4 compactions committed with 0 stale retries.

**Against a live summarizer the availability holds and the cost is much higher ✔.** The arm above scripts the summarize call at 800 ms, and 800 ms is precisely the quantity the design is built around, so we repeated it with a real model and a real summarizer, pinning the advertised context window to 12,288 tokens to create the same pressure (three users, eight rounds, 24 turns). A real summarize window is **56 s to 123 s**, not 0.8 s: two orders of magnitude longer, which is what makes running it outside the lock a design decision rather than a detail. Availability survives that stretch. Across the five windows, 45 turn events landed *inside* them including 11 first tokens, so users kept getting answers throughout multi-minute compactions, and 24 of 24 queries were recorded with none lost.

The price, however, is not what the mock arm suggested. Of five compactions, **2 committed and 3 went stale**, against 4 of 4 committed with zero retries on the mock.

That is the optimistic design paying for its own optimism: a two-minute unlocked window gives concurrent turns two minutes to invalidate the generation, where a 0.8 s window gives them almost none. Nothing is lost when this happens, because a stale pass is discarded and retried rather than committed wrongly, but the wasted work is a real cost and it is the mock that understated it. We therefore report the availability property from both arms and the retry rate from the live one.

The instrumentation also caught a real design flaw before that result. In the first build, concurrent turns' overflow fallback (evicting the oldest messages when the window is exceeded) bumped the generation during every summarize window, starving the optimistic commit: 1 of 6 compactions landed, with 5 stale retries, so summaries were paid for and discarded while raw history was evicted unsummarized (`out/n1-compaction-adversarial.json` ✔).

The fix lets concurrent callers tolerate the margin between the preventive target and the true limit while a compaction is in flight. We report the failure because it is the kind of interaction, compaction × concurrency, that cannot be discovered in a single-user system, and because the measurement plane of §5 is what found it.

### 6.7 RQ7: Attribution that is structural, not heuristic ✔

Under concurrency, the nearest earlier question is not necessarily the question an answer belongs to. Coagora therefore makes attribution structural. One turn runs in one thread, that thread stores the originating question id, and every record it creates carries the same `reply_to` value (§5). The mapping does not depend on what the model says.

We verified at scale (4 users × 25 rounds, 100 concurrent turns). The mint chain (queue entry → dispatched turn → thread → minted question id) was correct 100/100, and the final records formed a perfect bijection with the questions: 0 duplicated, 0 unmatched, 0 missing.

Structural attribution does not guarantee semantic focus. Because all turns see one transcript, a model may answer another participant's newer question. We expose the difference with a deterministic adversarial model that always answers the newest visible question. The content can address the wrong user even while `reply_to` remains correct. These are separate properties: the system can record exactly *which request created the turn* without controlling *which request the model chooses to follow*.

This happens with real models too, and not rarely. In the two live compaction runs of §6.6, whose concurrent users receive near-identical prose tasks distinguished only by a marker each is told to echo, **21 of 42** final answers carried a *different* turn's marker. Earlier drafts cited a 3-of-3 spot check as evidence that live models answer their own askers; that sample was too small to say anything, and the larger one says the opposite.

**The semantic rate is not a stable number, and we no longer report one ✔.** An earlier single run of this configuration recorded 38%, and we previously quoted that figure as a worst case. Repeating the identical configuration says otherwise: across five fresh repetitions the rate was 13%, 17%, 19%, 19% and 21%, and two further single runs on the same host gave 4% and 10%. The spread is not noise in the measurement but the quantity itself. Confusion can only occur where turns actually overlap in time, and how much they overlap is a property of host scheduling rather than of the contract, so a single draw from this distribution is not a bound and we withdraw the claim that it was.

What *is* stable is everything structural: across all fifteen runs of the ablation below, plus the run above, the mint chain and the `reply_to` bijection were perfect every time, with zero duplicates and zero unmatched. Attribution does not fluctuate; only what the model chooses to talk about does.

**A mitigation, and the honest limit of testing it on a mock ✔.** Reviewers of an earlier draft reasonably asked whether the semantic confusion can be reduced rather than merely recorded, so we implemented one: `--turn-scoping` adds a section to each turn's system prompt naming the request that turn is serving and stating that other participants' concurrent messages in the shared transcript are context rather than instructions. It is gated twice, on the flag and on the parallel contract, since under serialization a mid-run injected question is precisely what the turn should take up (§6.1). The measurements below are what made it the shipped default under the parallel contract; every `off` arm in this section disables it explicitly.

Measuring its *effect* is exactly what a scripted model cannot do, because compliance with an instruction is the thing being tested and the mock's compliance is something we would be writing ourselves. So we measured the bracket instead, five repetitions of each arm at four users and twenty-five rounds:

| Arm | Semantic mismatch (min / median / max over 5 runs) | `reply_to` bijection |
|---|---|---|
| `off`: no scoping (the configuration above) | 13% / 19% / 21% | perfect in 5 of 5 |
| `ignore`: scoping on, model never reads the system prompt | 1% / 21% / 26% | perfect in 5 of 5 |
| `honor`: scoping on, model follows it | 0% / 0% / 0% | perfect in 5 of 5 |

The `ignore` arm is a negative control and it passes: a model that cannot see the instruction is statistically indistinguishable from one that was never given it, which is what tells us the two live arms differ by compliance rather than by some incidental effect of a larger prompt. The `honor` arm is 0 in every run, and its value is narrower than it looks. It does not show that real models comply. It shows that the mechanism is sufficient *if* they do, and, less obviously, that the scope reached each of 500 concurrent turns (five repetitions of 4 users × 25 rounds) carrying that turn's own request and not a neighbour's.

That second property is not free: it is the exact surface where the session-global attribution field failed under concurrency and had to become thread-local (§5), and this is an end-to-end check that the same class of bug has not reappeared in the new section. Where real models sit between the two arms is the question a mock cannot reach, so we asked it directly.

**The live arm: a real model complies, and the confusion disappears ✔.** We repeated the `off`/`on` comparison against the on-premise model with no mock anywhere, twenty repetitions per arm, alternating arms so that drift in server load could not accumulate into one of them. Two users submit simultaneously under the parallel contract; their instructions differ only in a tag and a target filename, which we chose in order to test the mitigation against confusion at its most severe (a choice the realistic-pair arm below then shows was not the severe case we took it for).

Judgement is by side effect rather than by reading answers: we join each history record's `reply_to` (which request the turn was serving) against its recorded file paths, so "this turn wrote that user's files" is a fact about attribution, not an inference from what the workspace ended up containing. The distinction matters, and it is why we do not reuse §6.4's classifier: inferring cross-task work from the final file set cannot separate *did the neighbour's job* from *failed its own*.

| Arm | Turns that wrote another user's files | Turns that completed their own task | Runs with both tasks correct |
|---|---|---|---|
| `off` | 25 of 40 | 36 of 40 | 16 of 20 |
| `on` | **0 of 40** | **40 of 40** | **20 of 20** |

The elimination is not within reach of chance (two-sided Fisher's exact on 25/40 against 0/40: p = 2.2 × 10⁻¹⁰). We ran this comparison twice, on either side of the step-seam repair described in §5, and report the later run because it is the one on repaired code; the earlier run of the same size gives 26 of 40 against 0 of 40, so the headline replicates across two independent sets of forty turns per arm and the pooled figure is 51 of 80 against 0 of 80.

The two secondary counts are where the runs differ, and the difference is instructive: the earlier run separated them (34 against 40 own-task completions, p = 0.026; 14 against 20 both-correct runs, p = 0.020) while the later one does not (36 against 40, p = 0.12; 16 against 20, p = 0.11). We therefore claim for the guard only what both runs support, a direction rather than a separation: scoping never reduced own-task completion, and in both runs it raised it.

Three things make this more than a happy number. First, the arms are genuinely concurrent in both cases: turns are released through a barrier, and in the run reported here the shorter turn spans at least 53% of the longer one in every unscoped run and at least 61% in every scoped one, with medians of 89% and 78%, so the `on` arm's zero is not an artefact of turns failing to overlap. (The earlier run's floors are 65% and 67%, with medians of 80% and 74%; we quote the run whose counts the table reports.)

Second, the guard direction moved the right way. Prompt scoping could plausibly have made the model timid, refusing work it was unsure of; instead own-task completion rose, from 36 of 40 to 40 of 40 here and from 34 of 40 to 40 of 40 in the earlier run, and median turn span fell, from 92.5 s to 80.9 s here and from 79.0 s to 62.8 s earlier. The model was not doing less, it was doing less of the wrong thing.

Third, the failure mode we found is not the one §6.4's anecdote suggested. The dominant `off` pattern is not a turn abandoning its task for someone else's; it is a turn doing **both**, writing its own two files and its neighbour's as well, which is why 36 of 40 turns still completed their own work while 25 of 40 trespassed. Duplicated effort on a shared workspace is a quieter failure than a swapped task, and last-write-wins decides the contents.

**The confusable pair is not the worst case, and that is the finding ✔.** We designed those instructions to differ only in a tag and a filename precisely so that the mitigation would be tested against confusion at its most severe, and we described them that way in earlier drafts. That description was an assumption, so we measured it: a second off-arm of the same size, on the same code and host, in which the two users' requests are genuinely different work — one populates a parser's token and rule files, the other writes a tool's README intro and usage — differing in subject, filenames, line content and completion tag, and sharing only the shape of the work (two files of eight lines) so that spans and sample sizes stay comparable.

The realistic pair is *not* safer. It produced cross-user writes in **31 of 40** turns against the confusable pair's 25 of 40, with every one of its 20 runs containing at least one trespass; the two workloads are statistically indistinguishable (p = 0.22) and the point estimate runs the wrong way for the assumption we had made. Own-task completion was identical at 36 of 40, and the realistic turns were *shorter* (median 71.5 s against 92.5 s), so the higher rate is not bought by more time to wander.

The mechanism above explains why the assumption failed: the dominant error is doing **both** tasks, and two clearly distinct, non-conflicting requests are if anything an easier pair to merge into one to-do list than two requests that look like duplicates of each other.

This matters more for deployment than a low base rate would have. Cross-user contamination is not an artefact of contrived instructions that a careful team could avoid by phrasing requests distinctly; it is what sharing a transcript does to ordinary, clearly separated work.

The mitigation holds there too ✔. Running the realistic pair with `--turn-scoping` at the same size gives **0 of 40** turns writing another user's files against 31 of 40 without it (p = 3.8 × 10⁻¹⁴), with every one of the 20 runs clean where every one of the 20 unscoped runs was contaminated. The guard moves the same way and further than before: own-task completion 40 of 40 against 36, both tasks correct in 20 of 20 runs against 16, and median turn span *down* from 71.5 s to 55.7 s.

Overlap is verified here as well and is if anything stronger: the shorter turn spans at least 76% of the longer one in every unscoped run and at least 74% in every scoped one (medians 98% and 77%). Across both workloads the scoped arms total 0 of 80 turns against 56 of 80 unscoped on this model. The two arms together are the finding: sharing a transcript contaminates ordinary work at a high rate, and naming each turn's own request in its own prompt removed every instance this model produced — a totality the next paragraph bounds.

The endpoint also served a second model, Qwen3.6-35B-A3B, so we repeated the realistic workload unchanged for twenty repetitions per arm ✔. **Both the base rate and the mitigation changed with the model.** Without scoping, contamination was lower than on the dense model: 14 of 40 turns rather than 31 of 40 (p = 2.6 × 10⁻⁴). With scoping, it fell to 9 of 40 rather than zero; the dense model had 0 of 40 (p = 2.4 × 10⁻³).

Within the second model, the off/on difference was not statistically separable at this sample size (p = 0.32), although every secondary measure moved in the same direction. Own-task completion rose from 33 to 38 of 40 turns, and runs with both tasks correct rose from 13 to 18 of 20. Text-level misdirection also tracked the file effects: another task's completion tag appeared in 12 of 40 unscoped answers and 10 of 40 scoped answers. Median overlap remained high at 0.86 and 0.76, although the minimum fell to 0.18 because this model's turns were about six times shorter.

Two conclusions, at the strength the data supports. The contamination *phenomenon* is model-general: it has appeared on every model we have run it on, dense, sparse, and adversarial-mock. The *elimination* is not: it is a property of the model-mitigation pair, and a deployment cannot assume one prompt section closes the gap for whatever model it serves — both the base rate and the guard's sufficiency are deployment-measurable properties, and the harness that measures them is in the artifact. The semantic gap remains neither intrinsic to sharing a transcript nor confined to confusable instructions; what changed is that "tell the model which request is its own" is now a mitigation with a measured boundary rather than a fix.

**Snapshot staleness, measured ✔.** §4.3 named bounded staleness as the snapshot rule's known cost, and until this revision the paper disclosed it without a number. The context-sequence instrumentation added in §5 closes that: every snapshot and every context mutation carries a monotone sequence value, so a step's staleness, the count of mutations (other turns' queries arriving, their answers committing) that entered the shared context between that step's snapshot and its own commit, is an arithmetic difference with no joins. On the attribution workload above (four users, twenty-five rounds), five repetitions under the parallel contract put **498 of 500** steps stale by at least one mutation, with a median depth of 3 to 4 and a maximum of 5, under verified full overlap (peak concurrency 4 in every run); a serial control arm measured **0 of 200**, as construction requires.

Because staleness depth is set by how many commits land while a turn is thinking, and a scripted turn thinks for about a second where a real one thinks for a minute, this is a quantity a mock could plausibly misrepresent. We therefore repeated it against the live model (three users, four rounds, three repetitions, plus a serial control) ✔. Both structural facts hold: under the parallel contract **75 of 92** steps were stale, with peak concurrency 3 in every run, and the serial control was again **0 of 13**.

The distribution, however, is not the mock's. The live share is lower (82% against essentially 100%) and the live tail is *twice as deep* (maximum 10 mutations against 5, median 2 against 3 to 4). Some of that is configuration, since the mock arm runs more users and far more rounds and so keeps the context busier, but the direction of the tail is the effect a mock cannot show: a real turn holds its snapshot for a minute, and a minute is long enough for ten other commits to land behind it. We report the live figures as the deployment-relevant ones and keep the mock arm as the dense-traffic control.

The number to carry is not the share, which is a property of how saturated the session is exactly as this section's confusion rates are properties of overlap, but the shape: under genuine concurrency a turn's prompt routinely lags the context, by a couple of mutations typically and by ten at the tail, and every one of those stale snapshots is still a well-formed transcript. Freshness is the price the contract pays for never blocking inference. The price is now a distribution rather than a disclosure, and Limitation 5 carries it.

### 6.8 RQ8: Fairness under flooding ✔

One user submits five long turns back-to-back; four others each ask one short question (cap = 3, 5 repetitions). With the per-user gate (one active turn per user, waiters skipped for other users), short questions dispatched in **76 ms** median (p95 148 ms) while the flooder's own backlog waited behind itself (median 3.3 s), and the per-user single-active invariant held with 0 violations. With the gate ablated (pure FIFO plus cap), the flooder monopolized the slots and the same short questions waited **1,807 ms** median (p95 3,219 ms), 24× worse, with 20 violations of the single-active invariant.

**Against a live model the gap is three orders of magnitude wider ✔.** Scripted long turns last seconds, while real ones last minutes, so the mock understates what the gate protects. We repeated the experiment with one user submitting four multi-file tasks and three other users each asking one tool-free question. The cap was 3, with five repetitions per arm. This larger run supersedes an earlier two-repetition result with the same shape.

With the gate, short questions dispatched in a median of **4.8 ms**. The flooder's own backlog waited **83.3 seconds** behind itself, and the single-active invariant had zero violations. Without the gate, short questions waited **151.2 seconds** at the median and 198.2 seconds at p95. The gap is about 31,000× rather than the mock's 24× because users now queue behind real multi-file turns. The mock shows that the mechanism works reproducibly; the live arm shows that the gate can separate an immediate answer from one arriving two and a half minutes later.

We report one number in this arm that does not flatter the gate. Its p95 for short questions is 23.6 s, not 4.8 ms, because with a cap of 3 and three short users arriving together, the third still waits for a slot: the gate bounds how many slots *one* user may hold, and it does not manufacture capacity. That is the honest shape of the guarantee, and it is why §4.5 states the cap and the gate as two separate mechanisms.

Note that a naive Jain index computed over *all* users is *higher* in the ablated arm (0.85 versus 0.24): everyone is equally badly served. The gate is deliberately "unfair" to the flooder's backlog, and that asymmetry is the mechanism rather than a defect of it, so the per-class absolute waits are the honest report and a scalar fairness index is the misleading one.

**What the admission gate does not govern, measured ✔.** The gate above prices dispatch fairness; §4.4's effect lock is a second queue the gate cannot see, and its strict no-overtaking FIFO plus the exclusivity of shell makes one user's build a wall for another user's file effects. We priced that wall by driving the lock directly (the method of §6.4): user A alternates 1 s of thinking with an exclusive shell hold of 1 s or 5 s, three rounds, while user B writes a file every 200 ms; a baseline arm runs B alone for the same wall-clock.

B's effect wait is bimodal exactly as the mechanism predicts. The median is indistinguishable from the baseline (0.09 ms to 0.15 ms against 0.13 ms), because a write landing in A's think interval waits for nothing. The p95 is essentially one shell hold (996 ms against the 1 s hold, 4,992 ms against the 5 s one), because a write landing inside a hold waits out its remainder, and the maximum never exceeds a single hold (998 ms and 5,001 ms): with one shell-holding neighbour there is no deeper pileup, though more concurrent exclusive streams would queue in FIFO order and sum. This is §4.4's stated trade carried to its worst case and no further. The price of the hard fairness guarantee is bounded by one exclusive hold, it scales with hold length rather than with load, and the admission gate of this section neither causes it nor can remove it.

### 6.9 RQ9: Session lifecycle (suspend, resume, staying bounded) ✔

A shared session is only useful if it *persists*, through server restarts and through hundreds of turns. This is a durability smoke test rather than a long-horizon study: 204 turns is minutes of real team use, not days, and §7.6 keeps long-horizon capacity listed as unmeasured. Three users drove 204 turns through one session in four phases; between phases the server process was terminated and relaunched with `--resume` (three suspend/resume cycles).

Every check held. All 204 of 204 queries were present in the durable history after the final phase, with 0 lost across restarts. Question ids remained globally unique across resumes, because the resume path re-derives the id counter from the *full* history rather than from the compacted cache, so a restarted session never re-mints an id that compaction dropped from the working window but that remains attributed in the record. Each resumed phase processed its 51 new turns normally. Compaction kept the shared context bounded throughout: 10 commits (2 stale, 0 failed) across the run, with the post-compaction cache never exceeding 2,610 tokens. The session's context footprint is governed by the compactor, not by session age.

Durability is a property of the store rather than of the model, so the scripted arm is the right instrument for it at scale; we nevertheless confirmed the resume path once against the live model, at the size a live run affords ✔. Three users drove 27 turns across three phases with two suspend/resume cycles, and every check held there too: 27 of 27 queries in the durable history, 0 lost, ids unique across both resumes, each resumed phase processing its new turns normally. That arm does not exercise compaction, whose live behaviour §6.6 measures separately, because a session this short never approaches the window.

### 6.10 RQ10: Live-model validation and the real price of parallelism ✔

All numbers above use deterministic mock latencies. We spot-checked the headline and settled the cost question against a live on-premise model (Qwen3.6-27B, MLX 8-bit; twelve repetitions per contract for the ranking check, six for token accounting).

*Ranking preservation ✔.* User A starts a long generation; B asks a one-liner 2 s later. Over twelve repetitions per contract, B's time-to-first-token was a median of **38.2 s** under serial (B waits out A's entire turn) against **10.8 s** under parallel, a 3.5× separation. The dispersion is what makes this more than a spot check: serial spanned 37.8 s to 38.4 s and parallel 10.6 s to 10.9 s across all twelve runs each, so the two distributions are separated by an interval containing no observation from either (bootstrap 95% CIs of the medians: 38.0 s to 38.3 s against 10.7 s to 10.8 s). The parallel absolute is honest about what a live server adds; B's 10.8 s is real prefill plus concurrent-decode slowdown on shared inference hardware, not the harness's 0.29 s floor.

Repeating the whole comparison after the step-seam repair of §5, on a busier endpoint, moved both absolutes up by about 14% and left the finding intact: 43.6 s serial against 12.3 s parallel, ranges again non-overlapping (42.9 s to 44.3 s and 11.9 s to 12.5 s), a separation of 3.55× against the original 3.54×. The ratio is what the contract governs and the absolutes are what the endpoint's load governs, and the pair of runs separates the two cleanly.

*What bounds the parallel absolute, measured ✔.* We previously wrote that independence from A's *existence* is bounded by the provider's concurrency without saying what that concurrency is, which left the claim unfalsifiable. We measured it directly against the endpoint, bypassing the session entirely: N identical fixed-length generations released simultaneously, three repetitions each. Wall time was 8.2 s at N = 1, 13.7 s at N = 2, 15.5 s at N = 4 and 21.8 s at N = 8, so eight concurrent requests cost 2.68× one request rather than 8×, while aggregate throughput rose from 14.7 to 44.1 output tokens per second. The endpoint therefore does batch, but not for free: each individual request slows as concurrency rises.

This is the divisor for reading everything above. The session contract guarantees that B's request *reaches* the model without waiting for A; what fraction of the remaining latency B recovers is set by this curve, and no concurrency contract can move it.

*Token accounting, and a correction ✔.* The obvious fear is that N parallel turns pay about N times the inference cost. Measured on an identical three-question workload over six repetitions per contract: the parallel contract made 3 calls totaling **23.2K** input tokens; the serial contract made **2** calls totaling **15.6K**, a **1.49×** premium, not 3×.

Earlier drafts said that the token premium would grow with history length. The measurement shows that claim was wrong. With five prior turns already in the session, totals rose to 17.4K tokens for serial and 25.5K for parallel. The premium was **1.47×**, nearly the same as 1.49× on an empty session. The first call grew from about 7.7K to 8.6K input tokens, confirming that the added history was present.

The ratio stays flat for an arithmetic reason. If each of *c* calls reads a system prompt *S* and history *H*, then *c_parallel(S+H) / c_serial(S+H) = c_parallel / c_serial*. The history term cancels. A longer history makes both contracts more expensive in the same proportion; the premium is the call-count ratio.

A re-run on the repaired seam reproduces this to the token: 2 calls and 15,612 input tokens serial against 3 calls and 23,198 parallel on an empty session, a premium of 1.486× identical to the original, and 1.452× after five prior turns against the original's 1.466×. Token accounting is the one live measurement here that is essentially deterministic, because it counts prompt content rather than time.

Two mechanisms explain the gap between fear and measurement. First, per-call input is dominated by the fixed system prompt (about 7.7K of each call here), so cost tracks *call count*, not snapshot re-reads; the shared-context overlap that motivated the N× fear is a minor term until histories grow long.

Second, the serial contract quietly *batches*. Mid-run injection folds a queued question into an active turn, so three questions require only two calls. Parallel mode gives up this efficiency because one independent turn per question is what creates the latency independence in §6.1.

Injection works only at a turn boundary, so its cost benefit and latency behavior have the same cause. The live workload here was multi-step and offered frequent boundaries. The §6.1 task was one long generation with no boundary before completion. Serial mode therefore saves the most tokens in workloads where it blocks least, and saves nothing in the workloads where it blocks most.

The honest statement of the trade: parallelism's token premium is the ratio of call counts (at most N, and shrinking as serial batching fails under load), paid for latency independence; provider-side prompt caching, absent on this deployment, would shrink the per-call fixed term further.

### 6.11 RQ11: What do people notice and do in a first shared session? [TODO: study pending]

The experiments above establish the contract's behavior, but they cannot reveal what participants understood while using it. Logs can show that one user waited, that two turns overlapped, or that a turn touched another user's files. They cannot show whether a person noticed the event, how they explained it, or what they decided to do. We therefore designed a first-use study around five human-observable measures: awareness of a partner's current work (M1), detection of logged events (M2), attribution of those events (M3), formation of coordination norms (M4), and judgments about appropriate deployment (M5).

**Participants and ethics.** The target sample is four to six pairs of peer developers, excluding the authors and people with a direct conflict of interest. Each pair completes one approximately 85-minute session. Participants provide written consent before screen and audio recording begins, may stop at any time, and approve any quotation before publication. Raw `history.jsonl` text is deleted after analysis; only coded data and approved, anonymized quotations are retained.

[TODO after recruitment: report the final number of pairs and participants, relevant experience, recruitment route, compensation, pilot handling, exclusions or dropouts, and the institutional ethics/IRB determination. If fewer than four main-study pairs complete the protocol, relabel this section as pilot observations rather than a study result.]

**Design and tasks.** The main comparison is within-subject: each pair uses both the shipped serial contract and the parallel contract for 20 minutes, with order and task pair counterbalanced across teams. Turn scoping remains enabled in both conditions so the study compares contracts rather than knowingly mixing semantic contamination into the main contrast. Two participants work from separate laptops on the same LAN session and the same prepared TaskBook repository. Each receives only their own task card; learning the partner's task through the shared session is part of the awareness question.

After the two main conditions, every pair completes an eight-minute diagnostic condition, Module C: the parallel contract with turn scoping disabled. This intentionally limited condition tests whether participants detect the cross-user work that §6.7 records mechanically. It always appears last, after participants have learned the interface, and is disclosed in the consent form and explained during debriefing. We therefore use Module C to study detection, not to estimate a contamination rate or compare conditions causally.

**Procedure and measures.** Both participants submit their first requests simultaneously at the start of each block. At minutes 8 and 16 of each main condition, the facilitator administers a ten-second freeze probe: “What is your partner doing right now?” A fifth probe occurs halfway through Module C. Two independent coders compare each answer with the partner's logged activity and score it 2 (correct task area and current action), 1 (correct area but vague or incorrect action), or 0 (incorrect or “do not know”). Disagreements are resolved by consensus.

After each main condition, participants complete five seven-point items covering response speed, awareness of the partner, perceived wrong-request behavior, control, and willingness to collaborate in that setting, plus one open response. During a final event-based interview, the facilitator uses `turns.jsonl` and `history.jsonl` to select three to five concrete events, prioritizing cross-user file effects, long serial waits, interrupts or retries, compaction, and periods of maximum overlap. For each event, participants first report whether they remember it (M2: hit or miss), then explain what they thought happened (M3: agent, system/tool, partner, self, or other) and what they did. Reports of events absent from the logs are retained as false alarms. The interview then asks about partner awareness, interrupt authority, explicit or implicit coordination rules (M4), waiting behavior across contracts, and conditions for workplace use (M5).

**Analysis.** Logs serve only as the answer key for M1 and M2 and as a way to select interview prompts. They are not outcomes in this study: latency, overlap, and contamination rates are already measured at larger scale in §6.1–§6.10. Because the sample is small and qualitative, we will not run hypothesis tests. We will report M1 score distributions and “do not know” responses, M2 hits, misses, and false alarms by event type, independently coded M3 attributions, case-based M4 norms, and an M5 summary of willing, conditional, and unwilling deployment judgments. Condition questionnaires will be descriptive only. Negative and disconfirming cases take priority over a single pooled narrative.

#### Study completion and data quality

| Item | Result to report after the study |
|---|---|
| Main-study sample | [TODO: pairs, participants, completed sessions] |
| Condition exposure | [TODO: serial-first / parallel-first counts; completed A, B, and C blocks] |
| Collected evidence | [TODO: valid freeze probes, questionnaire responses, interview events, approved quotations] |
| Missing or excluded data | [TODO: count, reason, and handling; write “none” if none] |
| Coding agreement | [TODO: initial agreement for M1 and M3, disagreements, and consensus procedure actually used] |

#### M1: Awareness of the partner's work

| Condition | Valid probes | Score 0 | Score 1 | Score 2 | “Do not know” |
|---|---:|---:|---:|---:|---:|
| Serial | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |
| Parallel | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |
| Module C (parallel, unscoped) | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |

[TODO: Write one short results paragraph describing the distribution across teams and conditions. State what participants could and could not identify; do not infer a population-level contract effect. Add one counterexample if a pair behaved differently from the dominant pattern.]

#### M2 and M3: Detection and attribution of concrete events

| Logged event type | Events presented | Hits | Misses | False alarms | Attribution pattern |
|---|---:|---:|---:|---:|---|
| Cross-user file effect | [TODO] | [TODO] | [TODO] | [TODO] | [TODO: agent / system / partner / self / other] |
| Long serial wait | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |
| Interrupt, retry, compaction, or high overlap | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |
| Other participant-raised event | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |

[TODO: Report which event types participants noticed and which remained invisible. For Module C, state the number of mechanically confirmed cross-user effects and how many affected participants detected them. Then summarize how participants attributed the events. Include one approved quotation and one miss or false alarm that complicates the main pattern. Do not report the Module C contamination count as an occurrence-rate estimate.]

#### M4: Coordination norms and waiting behavior

[TODO: Describe two to four recurring or contrasting norms, such as task-area division, turn-taking, verbal announcements, monitoring the partner's stream, or decisions about interrupt authority. Separate explicit agreements from implicit routines. Explain what participants did during serial waits and how concurrent streams changed their attention. Include at least one pair-level case and one approved quotation.]

#### M5: Deployment fit

| Judgment | Participants | Main reasons or required safeguards |
|---|---:|---|
| Would use | [TODO] | [TODO] |
| Would use conditionally | [TODO] | [TODO] |
| Would not use | [TODO] | [TODO] |

[TODO: Summarize the tasks participants considered appropriate or inappropriate and the safeguards they required. Add one approved quotation. Keep these as first-use judgments, not claims about long-term adoption.]

#### Descriptive condition questionnaire

| Item (1–7) | Serial | Parallel |
|---|---|---|
| Response speed | [TODO: median and range] | [TODO: median and range] |
| Awareness of partner | [TODO] | [TODO] |
| Perceived wrong-request behavior | [TODO] | [TODO] |
| Sense of control | [TODO] | [TODO] |
| Willingness to collaborate | [TODO] | [TODO] |

[TODO: Add at most one paragraph connecting the descriptive questionnaire to M1–M5. Report no significance tests and do not use these ratings to claim improved team performance.]

**Scope of the claims.** This study can show what participants noticed, how they interpreted events, the norms they formed, and the settings in which they would or would not use a shared agent. It cannot establish that the parallel contract improves team performance, estimate latency or contamination rates, or predict long-term adoption. The systems experiments answer the first two quantitative questions; a longitudinal deployment would be needed for the third.

### 6.12 Correctness and threats to validity

The concurrency machinery is covered by the repository's test suite: 3,593 tests, of which 3,558 pass and 35 are skipped as environment-gated, with no failures in the run reported here (the property-based code-index flake noted in earlier drafts did not trigger). Coverage includes the turn registry and per-user gating, the turn-scoping section and its double gate, effect-lock scope and the compatibility matrix, context snapshot and atomic commit (including the step seam's pair atomicity under concurrent appends, §5, and the context-sequence staleness instrumentation, §6.7), sequence numbering and incremental replay, the read-only token's endpoint-by-endpoint authorization table (parameterized over every registered route, so a new endpoint that fails to declare a side fails the suite rather than defaulting open), and suspend/resume.

A separate opt-in suite of 53 tests drives a real headless browser against a real server for behaviors unit tests cannot reach, such as whether an element hidden by an attribute is actually hidden once author stylesheets apply; it caught a real defect in the read-only client during this work that source-level assertions had passed.

Both concurrency defects disclosed in §5 were found incidentally, one live and one while instrumenting for a different measurement, so we closed the class rather than the instances: every invariant §4 states was audited for whether some test drives it through the *production seam under concurrency*, not only through the primitive that implements it. Eleven of the twelve were already exercised at the seam, the atomic block commit's test being the §5 repair's own regression. The audit found one gap, the replay cursor's allocation-order invariant (§4.7, seq issued under the same lock that appends to the buffer), which the end-to-end replay experiment covers live (§6.5) but no concurrency test covered at the unit layer; it is now regression-locked with concurrent emitters. The audit also initially flagged the read-only route table as unverified, wrongly: the parameterized suite described above enforces it, which is itself a small argument for auditing with two readers.

**Threats to validity.** Mock-LLM timings exclude provider-side variance, mitigated by the live-model spot checks of §6.10 and by reporting the deterministic and live pair where both exist. The measurements come from one host and one filesystem; §6.2 states explicitly which of its numbers are platform properties and which are contract guarantees, and §6.1 states the one place where a session boundary is crossed. The token premium of §6.10 is the call-count ratio and is measured history-invariant (1.49× against 1.47×, §6.10); its absolute cost still grows with history, and the ratio approaches N× only as serial batching fails. Our workloads are synthetic; deriving a workload mix from field logs of real shared sessions is future work, and no such logs exist to draw on today (§2.3).

Finally, the systems experiments establish that the contract is implementable and measure its costs; they do not establish that teams collaborate better. Section 6.11 reserves the first human evidence for what participants notice, infer, and decide during first use. [TODO after the study: replace this status sentence with the final sample boundary and any study-specific threats, including first-use effects, pair familiarity, facilitator influence, and Module C's fixed-last order. Until then, this paper claims no observed human outcome.]

---

## 7 Discussion: What Sharing a Session Creates

The contract removes the structural limits of detached delegation, but a live shared session creates new coordination problems. Drawing on the CSCW theories in §2.5, we identify four. These are open research questions, not claims that Coagora has already solved.

### 7.1 Asymmetric grounding

Grounding theory assumes that collaborators build common ground together [36]. A persistent agent session breaks that symmetry. User A and the agent may spend thirty minutes making decisions, abandoning approaches, and changing files before user B joins. B can replay the exact transcript (§6.5), but access to a history is not the same as understanding it.

The session's common ground therefore becomes an artifact that some participants know better than others. Coagora's ordered and attributed transcript could support catch-up tools such as summaries, decision digests, or per-file histories. We do not yet know which interface closes the gap, or whether fully closing it is always desirable. Planned study S2 will stage mid-session joins under three catch-up treatments and measure comprehension, time to first productive intervention, and repetition of settled debates. The smaller first-use protocol in the artifact precedes that study.

[TODO after §6.11: connect the M1 awareness pattern and “do not know” responses to asymmetric grounding. State whether participants used the transcript, direct conversation, file state, or inference to understand their partner, and retain any counterexample.]

### 7.2 Multi-party control arbitration

Mixed-initiative principles explain when an agent should act or ask, but they usually assume one human [40]. With several people, every control action needs an owner: whose approval, whose interrupt, and whose undo?

Coagora uses a deliberately minimal rule. A participant may interrupt their own turn but not someone else's, and another person's active turn never disables their composer. A read-only role allows observation without action (§5). Other plausible policies include role-based override, quorum approval for destructive effects, or visible permission for anyone to stop any turn. SWE-chat reports that solo users intervene in 39% of available opportunities [3]. In a group, intervention may become redundant, diffused, or amplified. Turn ids can support all of these policies; study S3 asks which policy works in practice.

[TODO after §6.11: integrate M3 attributions and the interview evidence about interrupt authority. Report whether participants believed they were allowed to stop a partner's work and what policy they asked for; do not generalize beyond first use.]

### 7.3 Peer accountability at agent speed

Daryanto et al. found that people verify AI suggestions more carefully when a peer can see their use [23]. Their assistant worked at a human pace. An autonomous agent changes many files at machine speed, so peers watch a stream rather than a suggestion under the cursor.

Visibility may still encourage verification, or it may produce alarm fatigue because the stream is too fast to follow. Coagora broadcasts each turn to every participant and supports read-only observers, making both outcomes testable. Which one occurs remains unknown.

[TODO after §6.11: replace “remains unknown” with the M2 detection result. If Module C produced cross-user effects, state how many affected participants detected them and how they responded; if it produced none, report that the planned detection test was inconclusive.]

### 7.4 Articulation work is displaced, not destroyed

Coordination technology moves articulation work rather than eliminating it [39]. Messenger-based delegation requires teams to track which channel launched a task, avoid duplicate mentions, and reconcile several partial PRs. A shared session turns some of that labor into mechanism: the queue orders work, the lock orders effects, and `reply_to` records attribution.

New labor appears in its place. Participants must decide when to steer, agree on concurrency limits, and follow several output streams. We expect mechanized ordering to be cheaper than social ordering, but the defensible claim is displacement, not disappearance. Planned field study S1 will measure the new work directly.

[TODO after §6.11: add the observed M4 norms and waiting behaviors. Distinguish rules participants stated aloud from routines inferred by the researchers, and connect them to the serial/parallel condition without claiming a performance effect.]

### 7.5 Trust model: what sharing a context exposes

The same exposure risk we identified for messenger integrations (§2.2) also applies here. **The contract assumes mutually trusting participants.** It is designed for a team that already shares a repository and a room, not for unrelated or untrusted users.

This assumption follows from the design. Every participant's message enters the shared context and therefore appears in every other turn's prompt. Section 6.7 shows that this cross-user channel is active: models sometimes follow another user's request, and §6.4 observes the same behavior in file writes. We measured it as a correctness problem among collaborators. From a security perspective, it is also an injection channel that requires no exploit; typing in the shared session is enough to influence another turn.

It is worth separating what the contract does guarantee from what it leaves to a layer above it.

| Property | Status under this contract |
|---|---|
| Physical integrity of concurrent effects | **Guaranteed** by the effect lock (§4.2, §6.2): no interleaved writes regardless of participant intent |
| Turn ownership and interrupt authority | **Guaranteed** (§4.2): a participant cannot cancel another's turn; ownership is checked against the caller's connection |
| Attribution of a turn to its requester | **Guaranteed** structurally (§6.7): the record of who asked survives concurrency, and survives a model that answers the wrong question |
| Watch-without-acting participation | **Provided** by the read-only token (§5): every mutating endpoint and every transcript-exceeding read endpoint answers 403 |
| Workspace confinement | **Partial** (§7.6, item 7): directory-level path confinement, not container-level |
| Semantic isolation between participants' turns | **Not provided.** Any participant's text can steer any other participant's turn, because they share one context |
| Confidentiality between participants | **Not provided.** There are no per-participant views of the transcript; attaching means seeing everything |
| Defence against a malicious writing participant | **Out of scope.** Nothing prevents a participant from instructing the agent to read, exfiltrate, or destroy what the workspace contains |

The final three rows follow directly from using one context. Per-participant confidentiality would require partitioning *C*, which would return to isolate-and-merge. Semantic isolation would require controlling which parts of one prompt a model may treat as instructions, which this contract does not attempt.

Multi-user policy work [30, 31] can complement the contract here. Permission hierarchies could refine the current read-only/read-write split, and the effect gate is a natural enforcement point because every state change already passes through it. We leave this integration to future work.

Filesystem isolation and prompt isolation are separate boundaries. A container could prevent a turn from reaching files outside the workspace, but it would not stop one participant's text from redirecting another turn inside that workspace. A deployment for untrusted users needs both forms of isolation; Coagora claims neither.

[TODO after §6.11: use Module C only to discuss whether semantic failures were visible to participants. If failures went unnoticed, explain the implication for relying on users as a last line of defense; if they were detected, describe that observed defense without upgrading it to a guarantee.]

### 7.6 Limitations

Honest boundaries of the current system and results.

1. *Token cost, measured* (§6.10): on the same three-question workload, parallel mode uses 1.49× the input tokens of serial mode because it makes three calls instead of two. Serial mode can inject a queued question into a running turn; parallel mode gives up that batching efficiency to make latency independent. The premium is the call-count ratio and does not grow with history length, because history increases every call in both contracts proportionally. It approaches N× only when serial batching fails completely—the same condition in which serial blocking is worst (§6.1). Provider-side prompt caching, which our deployment did not use, would reduce absolute cost but not this ratio.
2. *Context capacity, partially resolved*: one shared context grows roughly N times faster. Compaction can run alongside concurrent turns without losing records (§6.6), but live summaries often become stale before they can commit. Three of five live passes went stale, compared with none of four scripted passes, because a live summary takes about two minutes rather than one second. We have not measured how a single context behaves under N sustained users for hours.
3. *Semantic conflicts remain*: serialization protects physical integrity, not meaning. If two turns disagree about one file, the later write wins. If they read the same snapshot and modify different files, they may jointly break an invariant even though the lock correctly allows both writes (§4.3). In both cases every write can be intact, ordered, and attributed while the repository is still wrong.

   A shared transcript creates another semantic failure: a turn may follow another user's request. An adversarial mock produced answer mismatches ranging from 4% to 21% across repeated runs, with an earlier run at 38%; the variation follows scheduling overlap (§6.7). Live models also wrote other users' files. The first observation was 1 of 12 runs (§6.4), while dedicated experiments found 25 to 31 of 40 turns (§6.7).

   `--turn-scoping` mitigates this behavior by naming each turn's request. On the primary live model, contamination fell from 25 of 40 turns to 0 of 40, and own-task completion improved. The result is not universal: on a second model it fell from 14 of 40 to 9 of 40. Distinct requests were not safer than similar ones; they produced 31 of 40 contaminated turns without scoping. Across both workloads on the primary model, the total was 56 of 80 without scoping and 0 of 80 with it. Prompt scoping is therefore a model-dependent mitigation, not semantic isolation.
4. *Lock envelope*: effects of processes a tool detaches into the background escape the serial executor.
5. *Snapshot staleness, measured* (§4.3, §6.7): 75 of 92 live-model steps used a prompt that lagged the shared context, by a median of 2 mutations and a maximum of 10. In the denser mock workload, 498 of 500 steps were stale by a median of 3–4 and a maximum of 5. Both serial controls measured zero. Staleness is bounded by the turn's duration, but live inference produces a deeper tail than the scripted harness. The system does not yet surface stale reads to the model or user.
6. *Single-instance*: the event bus, locks, and registries are in-process, so a session does not span machines.
7. *Isolation depth*: directory-level path confinement, not container-level; a deployment that admits untrusted participants needs isolation this system does not claim.
8. *Steer-with-interrupt* is unsupported on concurrent turns (pure cancellation only).
9. *Replay window*: incremental replay is exact within a bounded buffer (§6.5); a client absent longer than the window gets a correct but truncated transcript with an explicit notice, and the complete record stays on disk.

---

## 8 Conclusion

Today's coding agents remain single-player. Teams can delegate through @-mentions, but they cannot intervene during a run and receive no coordination rule when several people act at once. A live shared session has usually been treated as a choice between serializing everyone or forking their state.

Coagora shows a third option. It keeps inference parallel and orders only conflicting side effects. In our evaluation, a second user's TTFT becomes independent of the first user's task length (slope 0.00 versus 1.03). Forced overlap produces 8.2% torn writes without the lock and none with it. Attribution is exact for all 100 concurrent turns, all 180 replayed events match a continuously connected client, and users can cancel their own work without stopping anyone else's.

The baseline strengthens these results. Coagora already shipped the serial shared session, and serial mode remains its default. Parallel and reject modes were added as switches on the same implementation. Each experiment therefore compares contracts within the production path rather than comparing a new prototype with a reconstructed baseline.

The contract also has measurable costs. It uses 1.49× as many input tokens on the three-question workload. Effective parallelism falls to 1.10× when exclusive effects occupy almost 90% of a turn. Below a 50% effect share, finer lock granularity provides little benefit. The same instrumentation reports both these costs and the latency and integrity gains.

A shared session still leaves important human problems open. Late joiners have less grounding than established participants. Groups need control policies richer than “own your turn.” Peer visibility may create accountability or simply overload attention. These questions define the next stage of multiplayer-agent research. Coagora, its harness, and its raw data are available as a foundation for that work.

---

## References

[NOTE: master keeps this working format; the CHI submission derivative (`21-chi-submission-draft.md`) is the anonymized form, and ACM BibTeX normalization happens at the LaTeX stage from these verified entries. Every entry has been checked against its source, including the author lists of [1], [3], [5], [21], [22], [24] and [25] and the record status of [26], verified against arXiv, the ACM DL entry, the issue tracker, or the author page on 2026-08-08/09. No flags remain.]

1. A. Welter, N. Schneider, T. Dick, K. Weis, C. Tinnes, M. Wyrich, S. Apel. *From developer pairs to AI copilots: A comparative study on knowledge transfer.* arXiv:2506.04785, 2025.
2. V. Chen, A. Talwalkar, J. Brennan, G. Neubig. *Code with me or for me? How increasing AI automation transforms developer workflows.* arXiv:2507.08149, 2025.
3. J. Baumann, V. Padmakumar, X. Li, J. Yang, D. Yang, S. Koyejo. *SWE-chat: Coding agent interactions from real users in the wild.* arXiv:2604.20779, 2026.
4. GitHub Next. *Ace: Agent Collaboration Environment.* Technical preview, 2026. https://ace.githubnext.com/ (accessed 2026-08-05).
5. apstorenet. *Feature request: real-time multi-user collaboration on a single Claude Code session.* Claude Code issue #60082, GitHub, 18 May 2026. https://github.com/anthropics/claude-code/issues/60082
6. Anthropic. *What is Claude Tag.* Support documentation, 2026. https://support.claude.com/en/articles/15594475
7. GitHub. *About Copilot integrations* (thread-capture notice). Documentation, 2026.
8. LangChain. *Double texting.* LangGraph Platform documentation, 2026. https://docs.langchain.com/langgraph-platform/double-texting
9. CopilotKit. *OpenTag: threads & persistence architecture* (single active run per thread; 409 on concurrency). Documentation, 2026.
10. Anthropic. *Claude Code: run parallel sessions with worktrees.* Documentation, 2026. https://code.claude.com/docs/en/worktrees (each parallel session is a separate git worktree on its own branch, reviewed and merged afterwards).
11. H. Lyu, D. Zhang, M. Wu, X. Wei, H. Chen. *CoAgent: Concurrency control for multi-agent systems.* arXiv:2606.15376, 2026.
12. S. Imai. *Is GitHub Copilot a substitute for human pair-programming?* ICSE Companion 2022.
13. Anthropic. *Introducing Claude Tag.* 2026. https://www.anthropic.com/news/introducing-claude-tag
14. OpenAI. *Codex — now generally available.* 2026. https://openai.com/index/codex-now-generally-available/
15. OpenAI. *Codex Slack integration.* Documentation, 2026. https://learn.chatgpt.com/docs/third-party/slack
16. GitHub. *Integrate Copilot cloud agent with Slack.* Documentation, 2026. https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/integrate-cloud-agent-with-slack
17. Cognition. *Devin Slack integration.* Documentation, 2026. https://docs.devin.ai/integrations/slack
18. Cursor. *Cursor in Slack.* Documentation, 2026. https://cursor.com/docs/integrations/slack
19. MITRE ATLAS. *Case study AML.CS0035: Data exfiltration from Slack AI via indirect prompt injection.* 2024.
20. Lehmann, Shauchenka, Buschek. *Collaborative document editing with multiple users and AI agents.* CHI 2026. arXiv:2509.11826.
21. S. Houde, K. Brimijoin, M. Muller, S. I. Ross, D. A. Silva Moran, G. E. Gonzalez, S. Kunde, M. A. Foreman, J. D. Weisz. *Controlling AI agent participation in group conversations: A human-centered approach.* IUI 2025. arXiv:2501.17258.
22. J. Yang, K.-H. Lai, X. Wang, S. Chang, Y. Harari, E. Gabrilovich. *GroupMemBench: Benchmarking LLM agent memory in multi-party conversations.* arXiv:2605.14498, 2026.
23. Daryanto et al. *Human-human-AI triadic programming.* arXiv:2601.12134, 2026.
24. C. Nachuma, M. Zibran. *When AI teammates meet code review: Collaboration signals shaping the integration of agent-authored pull requests.* MSR 2026. arXiv:2602.19441.
25. Z. Z. Wang, J. Yang, K. Lieret, et al. *Position: Humans are missing from AI coding agent research.* Preprint, 2026. https://zorazrw.github.io/files/position-haicode.pdf (re-checked 2026-08-09: still no proceedings entry; cited as a preprint).
26. M. Liu, T. Chen, Z. Xu, X. Jiang, Y. Dong. *Multi-agent collaboration with state management* (STORM: state-oriented management; conflicting edits detected and resolved at write time on a shared codebase). arXiv:2605.20563, 2026. Preprint; no venue is stated on the record.
27. Y. Mao, A. Mirhoseini. *Decentralized multi-agent systems with shared context* (DeLM). arXiv:2606.10662, 2026.
28. M. Yang et al. *Justitia: Fair and efficient scheduling of task-parallel LLM agents with selective pampering.* arXiv:2510.17015, 2025.
29. M. Cim et al. *Parallel context compaction for long-horizon LLM agent serving.* arXiv:2605.23296, 2026.
30. S. Yang et al. *Multi-user large language model agents.* arXiv:2604.08567, 2026.
31. S. Yang et al. *ProACT: Towards breakdown-aware proactive agents in multi-user collaboration.* arXiv:2607.03730, 2026.
32. H. Berenson, P. Bernstein, J. Gray, J. Melton, E. O'Neil, P. O'Neil. *A critique of ANSI SQL isolation levels.* SIGMOD 1995.
33. J. N. Gray, R. A. Lorie, G. R. Putzolu, I. L. Traiger. *Granularity of locks and degrees of consistency in a shared data base.* IFIP Working Conference on Modelling in Data Base Management Systems, 1976.
34. P. A. Bernstein, N. Goodman. *Concurrency control in distributed database systems.* ACM Computing Surveys 13(2), 1981.
35. J. Gray, A. Reuter. *Transaction Processing: Concepts and Techniques.* Morgan Kaufmann, 1993.
36. H. H. Clark, S. E. Brennan. *Grounding in communication.* In Perspectives on Socially Shared Cognition, 1991.
37. P. Dourish, V. Bellotti. *Awareness and coordination in shared workspaces.* CSCW 1992.
38. C. Gutwin, S. Greenberg. *A descriptive framework of workspace awareness for real-time groupware.* JCSCW, 2002.
39. K. Schmidt, L. Bannon. *Taking CSCW seriously: Supporting articulation work.* JCSCW 1(1), 1992.
40. E. Horvitz. *Principles of mixed-initiative user interfaces.* CHI 1999.
41. D. Ledo, S. Houben, J. Vermeulen, N. Marquardt, L. Oehlberg, S. Greenberg. *Evaluation strategies for HCI toolkit research.* CHI 2018.

---

## Appendix A: Artifact availability (draft)

The system, its deterministic benchmark harness, and every raw measurement file are in one repository. The harness lives under `bench/multiuser/` and is standard-library only, driving the real server over HTTP against a Python mock LLM whose latencies and tool steps are scripted by an in-prompt directive. Each script writes its raw JSONL and a derived summary into `bench/multiuser/out/`, which is committed, so every number in §6 can be recomputed from the raw files without re-running anything.

| Script | Section | Needs |
|---|---|---|
| `e2_hol.py` | §6.1 head-of-line grid (3 contracts × 4 task lengths) | mock only |
| `e2b_injection.py` | §6.1 boundary-density axis (L fixed, k ∈ {1, 2, 4, 8}) | mock only |
| `e2c_retry.py` | §6.1 reject penalty against retry interval | mock only |
| `e2d_nscale.py` | §6.1 user-count axis (N ∈ {2, 4, 8}) | mock only |
| `e1_ablation.py` | §6.2 integrity ablation across lock scopes | mock only |
| `p2_grid.py` | §6.3 collapse boundary (write count, conflict, scope) | mock only |
| `p2_scope.py` | §6.4 conflict-scope recovery (effect share × paths × scope) | mock only |
| `p2_scope_real.py` | §6.4 real operating point (live model, effect share measured from lock instrumentation) | on-premise model endpoint |
| `n4_replay.py` | §6.5 incremental replay for late joiners | mock only |
| `n1_compaction.py` | §6.6 compaction under concurrent turns (`--real` adds the live-summarizer arm) | mock; `--real` needs the endpoint |
| `n3_attribution.py` | §6.7 structural attribution at scale | mock only |
| `n3b_scoping.py` | §6.7 turn-scoping ablation (off / ignore / honor × 5) | mock only |
| `n3c_scoping_real.py` | §6.7 turn-scoping against the live model, judged by per-turn file attribution; `--workload confusable\|realistic` selects the instruction pair | on-premise model endpoint |
| `n5_staleness.py` | §6.7 snapshot staleness (parallel vs serial control; `--real` for the live arm) | mock; `--real` needs the endpoint |
| `p4b_mixed_fairness.py` | §6.8 mixed-workload effect-layer wait (lock driven directly) | none (no server) |
| `stats_recompute.py` | §6 Fisher tests and bootstrap CIs from committed raw files | none (no server) |
| `compare_prepost.py` | §5 pre-fix against post-fix live results, side by side | none (no server) |
| `verify_paper_claims.py` | §6 every quoted number re-derived from the committed artifacts, 119 checks | none (no server) |
| `p6b_provider_concurrency.py` | §6.10 what the endpoint's own concurrency bounds | on-premise model endpoint |
| `p2_shell_real.py` | §6.4 shell-dominated operating point | on-premise model endpoint |
| `p4_fairness.py` | §6.8 per-user fairness ablation (`--real` for the live arm) | mock; `--real` needs the endpoint |
| `p7_lifecycle.py` | §6.9 204 turns across 3 suspend/resume cycles (`--real` for the reduced live confirmation) | mock; `--real` needs the endpoint |
| `p6_real_llm.py` | §6.10 live-model ranking and token accounting | on-premise model endpoint |

Scripts that need credentials (an OpenAI-compatible endpoint supplied through environment variables) are the live arms of §6.4, §6.7 and §6.10, plus the `--real` arms of `n1_compaction`, `n5_staleness`, `p4_fairness` and `p7_lifecycle`; every script runs offline in its default mock configuration, and the four `--real` scripts default to the mock. Each condition executes in a fresh temporary workspace with an isolated `HOME`, so a benchmark run cannot read or modify the operator's configuration. `e2_hol.py` supports `--append`, which merges a newly measured task length into the committed raw file and re-derives the summary from the union; `out/e2-drift-control.json` records the control measurement for the session boundary discussed in §6. [NOTE: replace with anonymized artifact links for double-blind review.]
