# Coagora: Sharing a Live Coding Agent Session Across Multiple Developers

> Full paper draft v1.1-wip (2026-08-13). This working master focuses the paper on the interaction and systems consequences of sharing one live coding-agent session. Detailed engineering history remains in 09-CHANGELOG.md, and reproduction material remains in Appendix A. The first-use study in §6 is reserved for later execution; its methods and result placeholders are intentionally unchanged in substance.
>
> Author notes appear as [NOTE: …] or [TODO: …] and must be removed before submission. Numbers marked ✔ are backed by committed raw data in bench/multiuser/out/.

---

## Abstract

Coding agents are designed around one person controlling one session. Teams can delegate through messenger integrations, but those integrations usually launch detached tasks. They do not provide a live shared workspace, and they rarely define what happens when several people instruct the agent at once. Sharing one session seems to require either serializing requests, which makes a short question wait behind a long task, or forking the session, which partitions context and creates merge work.

We present Coagora, a coding agent in which multiple developers share one conversation and one workspace. Its concurrency contract separates inference from effects: turns infer and stream in parallel, while context commits and conflicting workspace effects are ordered. Each turn remains linked to the message and participant that created it. This placement uses established concurrency-control techniques, but applies them below inference so that physical integrity does not require blocking another participant's reasoning.

We compare parallel, serial, and reject-and-retry contracts within the same implementation. In a deterministic workload, the second user's time-to-first-token is independent of the first user's task length: its slope is 0.00 under parallel execution and 1.03 under both alternatives. A live-model replication reduces the second user's median wait from 38.2 to 10.8 seconds. Forced overlap produces torn writes in 8.2% of sampled states without locking and none with either lock. Parallel execution costs 1.49× as many input tokens in a three-question workload, and its benefit declines only when exclusive effects occupy a large share of a turn.

Physical integrity does not imply semantic correctness. In a shared transcript, turns sometimes performed another participant's task even when requests were clearly different. A turn-scoped prompt eliminated all observed cross-user file effects on one model but left residual failures on another. Prompt scoping is therefore a mitigation, not isolation. These results show that a shared coding-agent session is technically viable, while making request ownership, stale context, and visible control central interaction-design problems.

[TODO after the first-use study: add one sentence reporting the central human finding from §6.]

---

## 1 Introduction

Coding agents now read repositories, edit files, run commands and tests, and open pull requests with limited supervision [1, 2]. Studies and telemetry show that they can complete substantial work, and that users frequently redirect them while they run [2, 3]. Yet the interaction model remains almost entirely single-player: one person owns the prompt, the context, and the controls.

Software development is collaborative. Several products now let teams mention an agent from Slack or Teams [6, 14–18], and users have requested Google-Docs-style access to a single coding-agent session [5]. The deployed messenger pattern is useful, but it is delegation rather than co-presence. A mention launches a cloud task from a captured thread, the agent works elsewhere, and a pull request returns later. Participants cannot usually intervene during execution, and published documentation does not provide a general rule for two people acting at once.

A live shared session would keep the conversation, workspace, and controls in one place. It also creates a coordination problem. If the system admits one request at a time, a quick question may wait behind a long build. If it creates a session per person, work can proceed concurrently, but context diverges and the team must merge both code and decisions.

Coagora explores a third contract: **parallel inference with ordered effects**. Every message creates an independent turn that begins immediately and streams to all participants. Turns share one conversation and one working directory. Slow, read-only inference remains concurrent; context commits and conflicting file or shell effects pass through an ordering layer. A turn id connects each stream, tool call, result, and interrupt to the participant who initiated it.

This is not a claim that locks or snapshot isolation are new. The contribution is their placement in an interactive agent. Prior agent systems often place coordination around the whole run and conclude that pessimistic control is too expensive because it blocks inference [11]. Coagora orders only the state-changing portion. The resulting system exposes a sharper boundary: the session can preserve a valid transcript and intact files while a model still follows another person's request.

We study three questions:

- **RQ1 — Responsiveness.** How do serial, reject-and-retry, and parallel contracts change one participant's wait when another participant is already working?
- **RQ2 — Integrity and cost.** Which effects must be ordered, when does ordering erase the benefit of parallel inference, and what token and runtime costs remain?
- **RQ3 — Shared-session correctness.** Do replay, compaction, attribution, fairness, and lifecycle remain correct under concurrency, and when does shared context cause semantic cross-talk?
- **RQ4 — First use.** What do pairs notice, infer, and do while sharing a live session? Section 6 reserves this question for the planned study.

The evidence comes from one working system with three runtime-selectable contracts. Coagora's serial path predates this work and provides the baseline; the parallel and reject modes use the same transport, context manager, tools, and instrumentation. Deterministic experiments verify mechanisms and boundaries, while live-model arms test the findings most likely to depend on model or provider behavior.

This paper contributes:

1. **A scoped design space** for multi-user coding-agent interaction, organized by state location, concurrency contract, attribution unit, and intervention point.
2. **A shared-session interaction architecture** that combines parallel turns with atomic context commits, conflict-scoped effects, per-user admission, structural attribution, replay, and turn-owned interruption.
3. **An empirical account of its trade-offs.** Parallel turns remove inference-level head-of-line blocking, but pay additional model calls, use stale snapshots, and can semantically absorb another participant's request. The evaluation identifies where those costs arise and which are guarantees versus model-dependent mitigations.

The planned first-use study will later add evidence about how people understand and manage these trade-offs. Until then, the paper makes no claim that the parallel contract improves team performance or long-term collaboration.

---

## 2 Related Work and Design Space

### 2.1 From individual agents to team access

Research on AI pair programming usually studies one developer with one assistant or agent. Copilot can increase output while reducing quality relative to human pairing [12], developers inspect AI suggestions differently from human suggestions [1], and understanding agent behavior remains a barrier to adoption [2]. In 6,000 real agent sessions, users interrupted or redirected the agent in 39% of available opportunities [3]. These findings establish that steering matters, but assume one person controls it.

Team access is emerging through messenger integrations. We reviewed publicly available documentation for Claude Tag, Codex, GitHub Copilot, Devin, and Cursor [6, 13–18], along with OpenTag [9], documented worktree-based parallelism [10], and Coagora's two modes. We included systems whose documentation was sufficient to identify where session state lives and what happens when a second request arrives. This is a scoped product and literature survey, not an exhaustive census; claims below refer to the surveyed systems.

The five messenger integrations follow a similar path: mention, thread capture, cloud task, and pull-request link. Identity and persistence differ, but the live interaction boundary is similar. GitHub warns that a full thread may be stored in the resulting pull request [7], and indirect prompt injection through Slack AI shows that treating a channel as model context has security consequences [19]. Claude Tag describes a shared channel instance as multiplayer, but does not publish a protocol for simultaneous instructions [6].

### 2.2 Multiple people around one AI

The closest human-centered work comes from collaborative writing, group conversation, and education. Agents placed in a multi-user writing environment became both personal workspaces and shared team resources [20]. Group-conversation research has studied when, where, and how an AI participant should respond [21], while GroupMemBench shows that memory remains difficult in multi-party conversations [22].

In programming education, Daryanto et al. compared one-human/one-AI and two-human/one-AI configurations [23]. Peer visibility encouraged participants to examine AI suggestions more carefully. Their system was suggestion-based and the task was a one-off learning exercise, rather than a persistent autonomous agent with file and shell access. Work on agent-authored pull requests likewise studies collaboration around an agent's output after execution [24], not several people sharing the live execution itself. Public human-agent interaction data is sparse more generally [25].

These studies motivate questions of awareness, accountability, and control, but do not supply a concurrency contract for a shared coding workspace.

### 2.3 Concurrency over shared agent state

Multi-agent systems usually coordinate shared state optimistically. CoAgent repairs bad ordering with saga-style undo [11], STORM detects conflicting edits at write time [26], and worktree-based tools isolate branches and merge later [10]. DeLM runs several agents over shared context, but verifies whether content is supported rather than ordering concurrent work [27]. These systems coordinate several agents for one goal. Coagora instead coordinates several people through one session.

Busy-thread frameworks take another path. LangGraph offers reject, enqueue, interrupt, and rollback [8]. OpenTag permits one active run per thread and returns 409 to another request [9]. Our reject-and-retry and serial arms correspond to the first two policies. Fair schedulers such as Justitia divide serving capacity among agent tasks [28], while ordinary gateways rate-limit clients. Coagora's narrower concern is fairness among turns submitted by co-present people competing for slots inside one session.

The implementation uses established database ideas. Immutable snapshots and atomic commits resemble snapshot isolation [32]; the resulting stale reads and write-skew risks remain. Resource classification and compatibility follow granular locking [33], and last-writer outcomes are familiar from concurrency control [34, 35]. The research question is not whether these mechanisms are novel, but whether placing them below inference produces a useful interactive boundary.

### 2.4 Four design questions

We compare the surveyed systems along four questions:

1. **State locus:** Is the human conversation a launcher for detached work, or a view onto the live session?
2. **Concurrency contract:** Does a second request get rejected, serialized, batched, isolated for later merge, or executed concurrently with ordered effects?
3. **Attribution unit:** Is output attributed at pull-request, task, message, or turn level?
4. **Intervention point:** Can a participant intervene only before or after a task, during the whole session, or in one owned turn?

| System | State locus | Concurrency contract | Attribution | Intervention |
|---|---|---|---|---|
| Claude Tag [6] | detached | undocumented for simultaneous input | task / organization | before or after |
| Codex Slack [14] | detached | task per mention | task | before or after |
| Copilot Slack [16] | detached | serial per channel by default | PR / requester | before or after |
| Devin Slack [17] | detached, synchronized | resume; serial within a session | task / mapped user | coarse thread reply |
| Cursor Slack [18] | detached | isolated VM per invocation | task | before or after |
| OpenTag [9] | live thread | reject with 409 | message | unavailable while busy |
| Parallel worktree tools [10] | per branch | isolate and merge | branch | per agent |
| Coagora serial | live session | serialize; inject at boundaries | turn | session steering |
| **Coagora parallel** | **live session** | **parallel with ordered effects** | **turn / requester** | **owned-turn cancel** |

Among these systems, messenger integrations cluster around detached state, task-level attribution, and pre/post intervention. None of the surveyed alternatives combines one live context, concurrent turns, per-turn attribution, and turn-scoped control. This table identifies the coordinate Coagora explores; it does not establish that no undocumented or future system can occupy it.

CSCW gives the coordinate its human meaning. Grounding describes how collaborators build common understanding [36]. Workspace awareness concerns knowing who is doing what and where [37, 38]. Articulation work is the labor required to coordinate cooperative work [39]. Mixed-initiative principles describe when an agent should act or ask [40], but a multi-user setting adds whom to ask and who may stop the action. These concepts guide the discussion and the planned study; the technical evaluation does not claim to measure them.

---

## 3 A Contract for One Shared Session

### 3.1 Session, turns, and ownership

A session is S = (C, W, T): one conversation context C, one working directory W, and a set of active turns T. Any number of clients may attach to the same process. Adding a participant does not create another workspace or context.

Every accepted message creates one turn. Concurrent messages are not batched into a fused prompt. Each turn receives a monotonically increasing id at dispatch, and its streamed text, tool calls, acknowledgements, errors, and durable records carry that id. A reply_to link connects the turn's records to the originating message. The interface can therefore render simultaneous streams as separate, labeled cards.

Control follows ownership. A participant may cancel their own active turn; the registry rejects attempts to cancel someone else's. Cancellation preserves partial output and releases the slot without stopping other turns. This is a minimal policy rather than a complete group-governance model.

[FIGURE TODO: Show two participants submitting concurrently, separate attributed streams, one shared context, and an effect gate. Include the serial timeline beside it for comparison.]

### 3.2 Shared context

Each inference step reads an immutable snapshot of C. Partial output from other in-flight turns is not visible. When a step finishes, its assistant message and corresponding tool results commit as one atomic block in completion order. This preserves the message-pairing constraints of chat APIs and ensures that the durable transcript corresponds to a valid serial commit order.

The trade-off is staleness. Another turn may commit after a snapshot is taken, so a running turn can reason over a well-formed but older transcript. Snapshot isolation also permits semantic write skew: two turns can read the same valid state, modify different files, and jointly violate a repository invariant. The contract preserves transcript structure and effect ordering, not the correctness of the model's reasoning.

### 3.3 Ordered effects

Inference is read-only and slow. Workspace effects are shorter, state-changing, and unsafe to interleave. Coagora therefore lets inference proceed concurrently while routing effects through a per-workspace compatibility gate.

| Effect pair | Policy | Reason |
|---|---|---|
| Reads or writes on different known paths | concurrent | resources do not conflict |
| Operations on the same path | ordered | preserve physical integrity |
| Shell, package, or delete versus any workspace effect | exclusive | the affected path set is unknown or can change directories |
| Composite or human-wait tools | lock at leaf effects | holding a parent lock could deadlock or block indefinitely |
| Unknown file effect | exclusive | safety-first fallback |

Paths are normalized for separators and filesystem case rules. Waiting effects follow strict FIFO without overtaking. This can delay a compatible operation behind an earlier exclusive waiter, but prevents a stream of small file operations from starving a shell command. A runtime switch selects no lock, a workspace-wide mutex, or conflict-scoped locking.

The contract does not merge or roll back conflicting writes. Same-resource operations run one after another, and the later write wins. This is sufficient for physical integrity but not semantic agreement.

### 3.4 Admission, replay, and lifecycle

Parallel sessions cap in-flight turns at four by default. Additional requests wait in a fair queue. A participant is eligible only when they have no other active turn, so one person cannot fill every slot with a backlog. This gate controls admission fairness; it cannot bypass an exclusive effect that is already holding the workspace gate.

Durable events receive monotonically increasing sequence numbers under the same lock that appends them to the replay buffer. Server-Sent Events use the sequence as Last-Event-ID, allowing a reconnecting browser to request only missed events. A stale, future, or previous-process cursor triggers an explicit reset and snapshot. A process epoch separates streams across suspend and resume.

Session state depends on the number of active turns rather than the last turn to finish. An idle process may suspend, leaving its history and workspace available for resume. The concurrency contract is fixed at process start so serial and parallel turns cannot coexist under different context rules.

### 3.5 Guarantees and boundaries

| Property | Status |
|---|---|
| Intact, non-overlapping conflicting effects | guaranteed by the effect gate |
| Well-formed, atomically committed context | guaranteed |
| Structural attribution to the requesting participant | guaranteed |
| Turn-owned cancellation | guaranteed |
| Exact incremental replay within the retained window | guaranteed |
| Freshest possible context for every turn | not guaranteed |
| Semantic isolation between participants' requests | not guaranteed |
| Repository-level correctness across disjoint effects | not guaranteed |
| Protection from malicious participants | out of scope |

This separation is central to the paper. “Integrity” below always means physical and structural integrity unless semantic correctness is named explicitly.

---

## 4 Implementation

Coagora is a Python coding agent with a terminal interface and a multi-user LAN web interface. Both front ends call the same agent loop through a renderer abstraction. One operating-system process owns a session, and concurrent turns run as threads over a shared heap.

The context manager provides immutable snapshots and atomic block commits. Context, renderer, and durable-append locks have one acquisition order to prevent cycles. Turn ownership and reply attribution are thread-local, so one turn cannot inherit the id of a newer request. Parallel turns rebuild their own system prompts; turn scoping appends the request that the current turn serves and tells the model to treat other participants' concurrent messages as context rather than instructions. It is enabled by default for the parallel contract and can be disabled for ablation.

Workspace effects announce an intent before execution. The effect gate maps that intent to a normalized resource and lock mode, records wait and hold time, and releases the mode when the operation finishes. Composite tools acquire the gate only at leaf effects. Durable appends also use a bounded striped lock: direct concurrent O_APPEND writes were intact on ext4 but corrupted records on a Windows-mounted WSL filesystem, so filesystem append behavior is not treated as portable.

The web client receives one SSE stream and sends occasional POST requests. SSE provides browser-native reconnection with Last-Event-ID. A shared write token permits action; an optional read-only token permits the transcript stream but receives 403 from every mutating endpoint and from reads that expose prompts, directives, or workspace contents. This is observation control, not participant-to-participant confidentiality.

With turn metrics enabled, the server writes structured lifecycle, effect, compaction, rejection, and token events using one monotonic clock. Metrics contain structural metadata rather than prompt or response text. Time-to-first-token begins at the earliest server record of the user's first attempt; in the reject condition this is the first 409, so retry delay is included.

The offline harness drives the real server over HTTP and replaces only the model with a deterministic OpenAI-compatible SSE endpoint. It scripts timing and tool steps through prompt directives, uses a fresh workspace and isolated HOME for every condition, and commits both raw JSONL and derived summaries. Live-model arms use an on-premise endpoint for questions that depend on model compliance, provider batching, or realistic turn duration.

The concurrency suite is covered by the repository tests, including turn admission, context atomicity, effect compatibility, attribution, replay, read-only authorization, and suspend/resume. A separate browser suite exercises client behavior. Appendix A maps every reported result to its reproduction script.

---

## 5 Technical Evaluation

We evaluate the contract as a systems and interaction substrate, not as evidence that teams collaborate better. Following evaluation guidance for HCI systems and toolkits [41], deterministic experiments verify mechanisms and boundaries, and live arms test deployment-sensitive findings.

All comparisons use the same binary and change one runtime switch. Count contrasts use two-sided Fisher tests, and median contrasts use fixed-seed percentile-bootstrap 95% confidence intervals when raw per-run samples are available. Inferential statistics describe repeated runtime observations; deterministic mechanism checks are interpreted as verification rather than population estimates.

### 5.1 RQ1: Responsiveness and its price

#### Head-of-line blocking

User A begins a task lasting L ∈ {2, 6, 15, 30} seconds. User B asks a one-line question 0.5 seconds later. We compare FIFO serialization, reject-and-retry at 250 ms, and parallel turns. Of 240 runs, 236 produced an attributable first token.

| Contract | L = 2 s | L = 6 s | L = 15 s | L = 30 s | dTTFT/dL |
|---|---:|---:|---:|---:|---:|
| Serial | 2.08 s | 6.16 s | 15.34 s | 30.86 s | 1.028 |
| Reject + retry | 2.31 s | 6.34 s | 15.42 s | 31.05 s | 1.027 |
| **Parallel** | **0.292 s** | **0.291 s** | **0.291 s** | **0.235 s** | **−0.002** |

The 30-second level was collected in a second session whose constant setup overhead was 56 ms lower; a same-session control reproduces the shift. Slopes and within-level comparisons are unaffected. Four runs with no attributable first token were excluded and reported in the raw data.

Under serial and reject contracts, each extra second of A's work adds about one second to B's wait. Reject adds a retry-phase penalty without improving the wait: at L = 15 seconds, 250 ms and 1,000 ms retry intervals added 172 ms and 794 ms over serial and required 61 and 16 attempts. Under parallel execution, B's first token depends on B's request and runtime overhead rather than A's task length.

The serial condition can inject a queued question at an internal tool boundary, so its true blocking unit is the **boundary interval**, not always the whole task. Holding L at 15 seconds and splitting A's work across k calls confirms this:

| Calls k | Boundary interval | Injection | B time-to-inclusion |
|---:|---:|---:|---:|
| 1 | 15.0 s | 0/10 | 15.10 s |
| 2 | 7.5 s | 10/10 | 7.30 s |
| 4 | 3.75 s | 10/10 | 3.39 s |
| 8 | 1.875 s | 10/10 | 1.44 s |

Time-to-inclusion is generous to serial because it stops when B enters a prompt, not when B receives an answer. Even at eight calls it remains 4.9× the parallel TTFT at the same total task length. Serialization works well when boundaries are frequent and approaches whole-task blocking when a generation, build, or test offers no boundary.

Increasing the total user count from 2 to 4 and 8, with the cap raised to avoid admission queuing, increased questioner TTFT from 236 to 265 and 328 ms. Seven simultaneous questioners therefore added 39% rather than 7×. This is runtime contention, not waiting for another user's task.

#### Live model and token cost

Against a live on-premise model, B asked a question two seconds after A began a long generation. Across twelve repetitions, median TTFT was **38.2 seconds serial and 10.8 seconds parallel**, with non-overlapping ranges and bootstrap intervals. A repeated run under heavier endpoint load preserved the ratio: 43.6 versus 12.3 seconds.

The provider still bounds the absolute benefit. Bypassing Coagora, eight simultaneous generations took 2.68× as long as one while aggregate throughput tripled. The contract ensures that B reaches the model; it cannot eliminate batching and decode contention inside the endpoint.

Parallel turns also use more calls. In an identical three-question workload, serial injection answered with two calls and 15.6K input tokens, while parallel execution used three calls and 23.2K tokens: a **1.49× premium**. With five prior turns, the ratio remained 1.47×. History increases both contracts proportionally; the premium is the call-count ratio. Parallelism exchanges serial batching efficiency for latency independence.

### 5.2 RQ2: Integrity and the effect boundary

#### What ordering prevents

Two writers repeatedly updated the same file with distinct markers and checksums while a 2 ms sampler classified the file. With locking disabled, 9 of 110 sampled states (8.2%) were torn writes containing both payloads. Workspace-wide and conflict-scoped locks produced 0 violations in 181 and 180 samples (9/110 versus 0/361, Fisher p = 1.6 × 10⁻⁶). The forced-overlap rate is platform- and implementation-specific; the guarantee is that conflicting effects do not overlap when the gate is active.

#### When a coarse lock becomes expensive

For two turns, fully serial effects impose no theoretical throughput penalty while the exclusive-effect share s is at most 0.5: one turn's inference can hide behind the other's effect. Above that knee, a workspace mutex increasingly dominates.

| Effect share | Paths | Workspace mutex | Conflict-scoped | Recovery |
|---:|---|---:|---:|---:|
| 25% | disjoint | 1.987 | 1.992 | 1.00× |
| 50% | disjoint | 1.974 | 1.983 | 1.01× |
| 75% | disjoint | 1.662 | 1.977 | 1.19× |
| 90% | disjoint | 1.368 | 1.983 | 1.45× |
| 90% | same path | 1.369 | 1.368 | 1.00× |

Same-path controls show no difference between scopes, while disjoint paths recover almost no-lock performance. The value of finer locking is therefore conditional: it matters for disjoint work only after exclusive effects occupy much of a turn.

Live file-writing turns sit far below this boundary. Eight writes spread across roughly four minutes produced an effect share of 10⁻⁵ and 0.1 ms median lock wait. Shell-heavy work moved the operating point: three one-second commands produced a 0.025 share and no measurable wait; three five-second commands produced a 0.094 share and about 4.1 seconds of wait. Even that workload remains below the 50% knee. File locks protect rare collisions; exclusive shell holds are the practical latency cost.

### 5.3 RQ3a: Structural correctness under concurrency

The following experiments test whether the supporting session machinery preserves its invariants. They are grouped because they validate one shared-session substrate rather than seven independent research contributions.

| Property | Experiment | Result |
|---|---|---|
| Replay | 90 turns; control stream versus 11 disconnect/reconnect cycles | 180/180 events identical in sequence, name, and payload; no missing, duplicate, or out-of-order event |
| Context compaction | 3 users × 30 rounds with an 800 ms summarizer | 90/90 queries retained; 4/4 compactions committed while 42 turn events arrived during unlocked summaries |
| Live compaction | 3 users × 8 rounds, 56–123 s summaries | 24/24 queries retained; turns continued; 2/5 compactions committed and 3 became stale |
| Attribution | 4 users × 25 rounds | 100/100 question-to-turn links formed a bijection; no duplicate, unmatched, or missing reply |
| Snapshot freshness | live: 3 users; mock: 4 users | live 75/92 steps stale, median depth 2, maximum 10; mock 498/500 stale, median 3–4, maximum 5; serial controls zero |
| Admission fairness | flooder plus short-question users | gate: 4.8 ms live median dispatch; no gate: 151.2 s; one active turn per user had no violations |
| Lifecycle | 204 turns across three suspend/resume cycles | 204/204 queries retained; ids unique; ten compactions; bounded cache; reduced live arm retained 27/27 |

Compaction illustrates the cost of optimism. Summarization runs outside the context lock so that turns continue, but a long live summary is often invalidated before commit. Stale work is discarded rather than committed incorrectly. Replay likewise has a clear boundary: incremental recovery is exact within the retained buffer; older or foreign cursors receive an explicit reset and snapshot.

Admission fairness and effect fairness are separate. The per-user gate prevents one person's backlog from filling all turn slots, but it cannot bypass a running exclusive shell. In a direct lock experiment, another user's file-write wait remained near baseline at the median and rose to approximately one shell hold at p95: 996 ms for a one-second hold and 4,992 ms for a five-second hold. Strict FIFO prevents starvation but exposes the cost of the exclusive operation.

Snapshot results quantify the price of never blocking inference. A concurrent turn routinely reasons over a valid but older context, and long live generations produce a deeper tail than the deterministic harness. The interface does not yet reveal this staleness.

### 5.4 RQ3b: Structural attribution is not semantic isolation

The system can know exactly which request created a turn while the model follows a different request. We first exposed this with an adversarial deterministic model: reply_to remained correct even when the content answered the newest visible question. Repeated mock runs produced different semantic mismatch rates as overlap changed, so we do not treat one mock rate as a deployment estimate.

The live experiments judged behavior by side effect. Each turn's reply_to was joined to the files it wrote, avoiding inference from final workspace contents. Two participants submitted simultaneously under the parallel contract.

| Model and workload | Cross-user file effects, unscoped | Cross-user file effects, scoped | Own task completed, off → on |
|---|---:|---:|---:|
| Primary model, similar tasks | 25/40 | **0/40** | 36/40 → 40/40 |
| Primary model, distinct realistic tasks | 31/40 | **0/40** | 36/40 → 40/40 |
| Second model, distinct tasks | 14/40 | **9/40** | 33/40 → 38/40 |

The similar-task comparison was independently repeated before the final implementation seam was repaired and produced 26/40 versus 0/40, preserving the result. In the primary model's two reported workloads, unscoped turns crossed into another participant's files 56 times in 80 turns, while scoped turns did so zero times. The dominant failure was often not replacing one's own task but doing both tasks, creating duplicated or last-writer-wins work.

Distinct tasks were not safer than nearly identical ones. One participant populated parser files while the other wrote tool documentation, yet the cross-user rate was numerically higher. Shared context can make separate requests look like one combined to-do list.

Turn scoping names the request that a turn serves and marks other concurrent requests as context. A deterministic negative control confirms that merely enlarging the prompt does not change behavior when the model ignores the section. On the primary live model, scoping eliminated every observed cross-user file effect without lowering own-task completion or overlap. On the second model, it left 9 of 40 affected turns, and the within-model difference was not statistically separable at this sample size. The phenomenon appeared across tested models; elimination did not.

This is the paper's central boundary. The contract guarantees physical ordering and structural ownership. Prompt scoping can improve semantic focus, but its sufficiency is a property of the model, workload, and prompt. A deployment must measure it and should add effect-level safeguards rather than treating prompt text as an isolation boundary.

### 5.5 Validity and claim boundaries

Deterministic timings remove provider variance but cannot represent model compliance or real serving contention. We therefore use them for mechanism verification and controlled sweeps, and use live models for responsiveness, prompt scoping, token use, compaction duration, fairness magnitude, staleness, and lifecycle confirmation. The live evidence still comes from one on-premise endpoint, two models for semantic scoping, one host, and mostly two or three users.

Workloads are synthetic and chosen to expose overlap and boundaries. The technical results establish that the contract is implementable and identify failure modes; they do not describe the distribution of tasks in production teams. The 8.2% torn-write figure belongs to a forced-overlap probe, while semantic-contamination counts belong to the tested model/workload pairs. Neither is a universal occurrence rate.

The repository reports 3,593 tests: 3,558 pass and 35 are environment-gated skips, with no failures in the reported run. A 53-test headless-browser suite covers behavior that source-level tests cannot reach. These suites increase confidence in the implementation but do not replace external replication.

Most importantly, no technical metric establishes better collaboration. Section 6 reserves the first human evidence. Until that study is complete, claims about awareness, coordination, control, and deployment fit remain research questions.

---

## 6 RQ4: First Use of a Shared Session [TODO: study pending]

The experiments above establish the contract's behavior, but they cannot reveal what participants understood while using it. Logs can show that one user waited, that two turns overlapped, or that a turn touched another user's files. They cannot show whether a person noticed the event, how they explained it, or what they decided to do. We therefore designed a first-use study around five human-observable measures: awareness of a partner's current work (M1), detection of logged events (M2), attribution of those events (M3), formation of coordination norms (M4), and judgments about appropriate deployment (M5).

**Participants and ethics.** The target sample is four to six pairs of peer developers, excluding the authors and people with a direct conflict of interest. Each pair completes one approximately 85-minute session. Participants provide written consent before screen and audio recording begins, may stop at any time, and approve any quotation before publication. Raw history text is deleted after analysis; only coded data and approved, anonymized quotations are retained.

[TODO after recruitment: report the final number of pairs and participants, relevant experience, recruitment route, compensation, pilot handling, exclusions or dropouts, and the institutional ethics/IRB determination. If fewer than four main-study pairs complete the protocol, relabel this section as pilot observations rather than a study result.]

**Design and tasks.** The main comparison is within-subject: each pair uses both the shipped serial contract and the parallel contract for 20 minutes, with order and task pair counterbalanced across teams. Turn scoping remains enabled in both conditions so the study compares contracts rather than knowingly mixing semantic contamination into the main contrast. Two participants work from separate laptops on the same LAN session and the same prepared TaskBook repository. Each receives only their own task card; learning the partner's task through the shared session is part of the awareness question.

After the two main conditions, every pair completes an eight-minute diagnostic condition, Module C: the parallel contract with turn scoping disabled. This intentionally limited condition tests whether participants detect the cross-user work that §5.4 records mechanically. It always appears last, after participants have learned the interface, and is disclosed in the consent form and explained during debriefing. We therefore use Module C to study detection, not to estimate a contamination rate or compare conditions causally.

**Procedure and measures.** Both participants submit their first requests simultaneously at the start of each block. At minutes 8 and 16 of each main condition, the facilitator administers a ten-second freeze probe: “What is your partner doing right now?” A fifth probe occurs halfway through Module C. Two independent coders compare each answer with the partner's logged activity and score it 2 (correct task area and current action), 1 (correct area but vague or incorrect action), or 0 (incorrect or “do not know”). Disagreements are resolved by consensus.

After each main condition, participants complete five seven-point items covering response speed, awareness of the partner, perceived wrong-request behavior, control, and willingness to collaborate in that setting, plus one open response. During a final event-based interview, the facilitator uses the metric and history logs to select three to five concrete events, prioritizing cross-user file effects, long serial waits, interrupts or retries, compaction, and periods of maximum overlap. For each event, participants first report whether they remember it (M2: hit or miss), then explain what they thought happened (M3: agent, system/tool, partner, self, or other) and what they did. Reports of events absent from the logs are retained as false alarms. The interview then asks about partner awareness, interrupt authority, explicit or implicit coordination rules (M4), waiting behavior across contracts, and conditions for workplace use (M5).

**Analysis.** Logs serve only as the answer key for M1 and M2 and as a way to select interview prompts. They are not outcomes in this study: latency, overlap, and contamination rates are already measured at larger scale in §5. Because the sample is small and qualitative, we will not run hypothesis tests. We will report M1 score distributions and “do not know” responses, M2 hits, misses, and false alarms by event type, independently coded M3 attributions, case-based M4 norms, and an M5 summary of willing, conditional, and unwilling deployment judgments. Condition questionnaires will be descriptive only. Negative and disconfirming cases take priority over a single pooled narrative.

### 6.1 Study completion and data quality

| Item | Result to report after the study |
|---|---|
| Main-study sample | [TODO: pairs, participants, completed sessions] |
| Condition exposure | [TODO: serial-first / parallel-first counts; completed A, B, and C blocks] |
| Collected evidence | [TODO: valid freeze probes, questionnaire responses, interview events, approved quotations] |
| Missing or excluded data | [TODO: count, reason, and handling; write “none” if none] |
| Coding agreement | [TODO: initial agreement for M1 and M3, disagreements, and consensus procedure actually used] |

### 6.2 M1: Awareness of the partner's work

| Condition | Valid probes | Score 0 | Score 1 | Score 2 | “Do not know” |
|---|---:|---:|---:|---:|---:|
| Serial | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |
| Parallel | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |
| Module C (parallel, unscoped) | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |

[TODO: Write one short results paragraph describing the distribution across teams and conditions. State what participants could and could not identify; do not infer a population-level contract effect. Add one counterexample if a pair behaved differently from the dominant pattern.]

### 6.3 M2 and M3: Detection and attribution

| Logged event type | Events presented | Hits | Misses | False alarms | Attribution pattern |
|---|---:|---:|---:|---:|---|
| Cross-user file effect | [TODO] | [TODO] | [TODO] | [TODO] | [TODO: agent / system / partner / self / other] |
| Long serial wait | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |
| Interrupt, retry, compaction, or high overlap | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |
| Other participant-raised event | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |

[TODO: Report which event types participants noticed and which remained invisible. For Module C, state the number of mechanically confirmed cross-user effects and how many affected participants detected them. Then summarize how participants attributed the events. Include one approved quotation and one miss or false alarm that complicates the main pattern. Do not report the Module C contamination count as an occurrence-rate estimate.]

### 6.4 M4: Coordination norms and waiting behavior

[TODO: Describe two to four recurring or contrasting norms, such as task-area division, turn-taking, verbal announcements, monitoring the partner's stream, or decisions about interrupt authority. Separate explicit agreements from implicit routines. Explain what participants did during serial waits and how concurrent streams changed their attention. Include at least one pair-level case and one approved quotation.]

### 6.5 M5: Deployment fit

| Judgment | Participants | Main reasons or required safeguards |
|---|---:|---|
| Would use | [TODO] | [TODO] |
| Would use conditionally | [TODO] | [TODO] |
| Would not use | [TODO] | [TODO] |

[TODO: Summarize the tasks participants considered appropriate or inappropriate and the safeguards they required. Add one approved quotation. Keep these as first-use judgments, not claims about long-term adoption.]

### 6.6 Descriptive condition questionnaire

| Item (1–7) | Serial | Parallel |
|---|---|---|
| Response speed | [TODO: median and range] | [TODO: median and range] |
| Awareness of partner | [TODO] | [TODO] |
| Perceived wrong-request behavior | [TODO] | [TODO] |
| Sense of control | [TODO] | [TODO] |
| Willingness to collaborate | [TODO] | [TODO] |

[TODO: Add at most one paragraph connecting the descriptive questionnaire to M1–M5. Report no significance tests and do not use these ratings to claim improved team performance.]

**Scope of the claims.** This study can show what participants noticed, how they interpreted events, the norms they formed, and the settings in which they would or would not use a shared agent. It cannot establish that the parallel contract improves team performance, estimate latency or contamination rates, or predict long-term adoption.

---

## 7 Discussion

### 7.1 One shared session changes the coordination problem

Detached delegation requires people to track which task was launched where and reconcile outputs later. A live session moves some of that articulation work into mechanisms: the queue admits turns, the effect gate orders state changes, and reply_to records ownership. The work does not disappear [39]. Participants must still divide tasks, monitor concurrent streams, decide when to interrupt, and repair semantic overlap.

A persistent transcript also creates asymmetric grounding [36]. A late participant can replay the same records, but access to history is not equivalent to understanding why decisions were made. Sequence and attribution make catch-up interfaces possible—decision summaries, per-file histories, or turn filters—but this paper does not test them.

Control becomes multi-party as well. “A user may stop their own turn” is predictable and prevents one participant from cancelling another's work, but it is insufficient for shared destructive effects. Teams may instead want role-based override, visible consent, or quorum approval. Turn ids provide a unit on which such policies can operate.

[TODO after §6: connect observed awareness, interrupt expectations, and coordination norms to this section without generalizing beyond first use.]

### 7.2 Design implications

The evaluation suggests four principles for shared-agent interfaces.

1. **Show request ownership separately from model focus.** A correct author label does not prove that the model followed that person's request. Interfaces should reveal both the requesting turn and the files or commands it affected.
2. **Surface stale context.** A turn can be structurally valid while several commits behind. A visible snapshot age or “newer activity exists” indicator would let users decide whether to continue, refresh, or cancel.
3. **Place safeguards at effects, not only prompts.** Prompt scoping is cheap and useful, but model-dependent. Cross-owned file edits, destructive commands, or unexpectedly broad effects can trigger previews, confirmation, or turn-specific capability scopes.
4. **Make waiting legible.** A user may wait for an inference slot, an exclusive effect, or a provider. These waits have different causes and remedies; one generic busy indicator hides the contract.

These are implications derived from measured system behavior. Whether users understand or value them remains for the first-use and later field studies.

### 7.3 Trust and deployment boundary

Coagora assumes mutually trusting collaborators who already share a repository. Every participant's message enters the shared context, so any participant's text may influence another turn. There is no participant-private transcript and no defense against a malicious writer. The read-only token separates watching from acting, but not one participant's information from another's.

Filesystem isolation and prompt isolation are also different. Directory confinement does not provide container isolation, and a container would not stop another participant's request from steering a turn inside that workspace. Deployment with untrusted users requires both a stronger execution boundary and multi-principal policy [30, 31].

### 7.4 Limitations

- **Short, synthetic workloads.** The experiments expose mechanisms and boundaries rather than the frequency of events in production work.
- **Narrow deployment environment.** Live results come from one endpoint, one host, and two models for the semantic mitigation. Provider behavior can change absolute latency.
- **Token cost.** Parallel execution used 1.49× the input tokens in the measured workload because it used three calls where serial injection used two.
- **Context growth and stale compaction.** A shared history grows faster with more participants. Turns remain available during compaction, but 3 of 5 long live summaries became stale before commit.
- **Semantic correctness.** Ordered writes can still implement incompatible decisions, and prompt scoping does not guarantee request isolation.
- **Lock envelope.** Effects from detached background processes escape the in-process gate; shell operations remain coarse and exclusive.
- **Single process and directory-level isolation.** Sessions do not span machines and are not containers.
- **Bounded replay.** Incremental replay is exact only within the retained event window; older clients receive an explicit reset.
- **Control policy.** Concurrent turns support cancellation, not mid-turn steering, shared undo, or group approval.
- **No human outcome yet.** The planned study can characterize first use but will not establish long-term adoption or team productivity.

---

## 8 Conclusion

Sharing a coding agent is not only a question of adding more clients to one chat. The system must decide when requests run, who owns each action, which state can change concurrently, and what participants can safely infer from a shared transcript.

Coagora keeps inference parallel while ordering context commits and conflicting effects. Within one implementation, this removes inference-level head-of-line blocking and preserves physical integrity across replay, compaction, attribution, fairness, and lifecycle operations. The cost is additional model calls, stale snapshots, and exclusive waits when effects dominate.

The harder boundary is semantic. A transcript and workspace can remain physically valid while a model follows another participant's request. Turn scoping removed observed cross-user file effects on one model but not another, so prompt text cannot serve as an isolation guarantee. A viable shared-agent interface must expose ownership, freshness, and effects clearly enough for people to understand and control that boundary. The planned first-use study will test how visible and manageable it is in practice.

---

## References

[NOTE: ACM BibTeX normalization happens at the LaTeX stage from these source-verified entries. Recheck product documentation and anonymization immediately before submission.]

1. A. Welter, N. Schneider, T. Dick, K. Weis, C. Tinnes, M. Wyrich, S. Apel. *From developer pairs to AI copilots: A comparative study on knowledge transfer.* arXiv:2506.04785, 2025.
2. V. Chen, A. Talwalkar, J. Brennan, G. Neubig. *Code with me or for me? How increasing AI automation transforms developer workflows.* arXiv:2507.08149, 2025.
3. J. Baumann, V. Padmakumar, X. Li, J. Yang, D. Yang, S. Koyejo. *SWE-chat: Coding agent interactions from real users in the wild.* arXiv:2604.20779, 2026.
4. GitHub Next. *Ace: Agent Collaboration Environment.* Technical preview, 2026. https://ace.githubnext.com/
5. apstorenet. *Feature request: real-time multi-user collaboration on a single Claude Code session.* Claude Code issue #60082, 2026. https://github.com/anthropics/claude-code/issues/60082
6. Anthropic. *What is Claude Tag.* Support documentation, 2026. https://support.claude.com/en/articles/15594475
7. GitHub. *About Copilot integrations.* Documentation, 2026.
8. LangChain. *Double texting.* LangGraph Platform documentation, 2026. https://docs.langchain.com/langgraph-platform/double-texting
9. CopilotKit. *OpenTag: threads & persistence architecture.* Documentation, 2026.
10. Anthropic. *Claude Code: run parallel sessions with worktrees.* Documentation, 2026. https://code.claude.com/docs/en/worktrees
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
25. Z. Z. Wang, J. Yang, K. Lieret, et al. *Position: Humans are missing from AI coding agent research.* Preprint, 2026.
26. M. Liu, T. Chen, Z. Xu, X. Jiang, Y. Dong. *Multi-agent collaboration with state management.* arXiv:2605.20563, 2026.
27. Y. Mao, A. Mirhoseini. *Decentralized multi-agent systems with shared context.* arXiv:2606.10662, 2026.
28. M. Yang et al. *Justitia: Fair and efficient scheduling of task-parallel LLM agents with selective pampering.* arXiv:2510.17015, 2025.
29. M. Cim et al. *Parallel context compaction for long-horizon LLM agent serving.* arXiv:2605.23296, 2026.
30. S. Yang et al. *Multi-user large language model agents.* arXiv:2604.08567, 2026.
31. S. Yang et al. *ProACT: Towards breakdown-aware proactive agents in multi-user collaboration.* arXiv:2607.03730, 2026.
32. H. Berenson, P. Bernstein, J. Gray, J. Melton, E. O'Neil, P. O'Neil. *A critique of ANSI SQL isolation levels.* SIGMOD 1995.
33. J. N. Gray, R. A. Lorie, G. R. Putzolu, I. L. Traiger. *Granularity of locks and degrees of consistency in a shared data base.* IFIP Working Conference on Modelling in Data Base Management Systems, 1976.
34. P. A. Bernstein, N. Goodman. *Concurrency control in distributed database systems.* ACM Computing Surveys 13(2), 1981.
35. J. Gray, A. Reuter. *Transaction Processing: Concepts and Techniques.* Morgan Kaufmann, 1993.
36. H. H. Clark, S. E. Brennan. *Grounding in communication.* In *Perspectives on Socially Shared Cognition*, 1991.
37. P. Dourish, V. Bellotti. *Awareness and coordination in shared workspaces.* CSCW 1992.
38. C. Gutwin, S. Greenberg. *A descriptive framework of workspace awareness for real-time groupware.* JCSCW, 2002.
39. K. Schmidt, L. Bannon. *Taking CSCW seriously: Supporting articulation work.* JCSCW 1(1), 1992.
40. E. Horvitz. *Principles of mixed-initiative user interfaces.* CHI 1999.
41. D. Ledo, S. Houben, J. Vermeulen, N. Marquardt, L. Oehlberg, S. Greenberg. *Evaluation strategies for HCI toolkit research.* CHI 2018.

---

## Appendix A: Artifact availability (draft)

The repository contains the implementation, deterministic harness, raw JSONL, and derived summaries. Offline scripts use the real server and a deterministic mock model; live arms require an OpenAI-compatible endpoint. Each condition runs in a fresh temporary workspace with an isolated HOME.

| Script | Evidence |
|---|---|
| e2_hol.py, e2b_injection.py, e2c_retry.py, e2d_nscale.py | §5.1 head-of-line, boundary density, retry, and user scaling |
| e1_ablation.py | §5.2 forced-overlap integrity |
| p2_grid.py, p2_scope.py, p2_scope_real.py, p2_shell_real.py | §5.2 effect boundary and live operating points |
| n4_replay.py | §5.3 reconnect and replay |
| n1_compaction.py | §5.3 mock and live compaction |
| n3_attribution.py, n3b_scoping.py, n3c_scoping_real.py | §5.4 attribution and turn scoping |
| n5_staleness.py | §5.3 snapshot staleness |
| p4_fairness.py, p4b_mixed_fairness.py | §5.3 admission and effect-layer fairness |
| p7_lifecycle.py | §5.3 suspend/resume lifecycle |
| p6_real_llm.py, p6b_provider_concurrency.py | §5.1 live ranking, tokens, and provider concurrency |
| stats_recompute.py | Statistical recomputation from committed raw files |
| verify_paper_claims.py | Re-derivation of quoted numerical claims |

[NOTE: Replace repository paths with anonymous archival links for double-blind review. The main paper must remain understandable without the artifact.]
