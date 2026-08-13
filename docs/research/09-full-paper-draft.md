# Coagora: Sharing a Live Coding Agent Session Across Multiple Developers

> Full paper draft v1.3 (2026-08-13). Detailed engineering history remains in 09-CHANGELOG.md, and reproduction material remains in Appendix A. Section 6 reserves space for the planned first-use study.
>
> Author notes appear as [NOTE: …] or [TODO: …] and must be removed before submission. Numbers marked ✔ are backed by committed raw data in bench/multiuser/out/.

---

## Abstract

Coding agents usually assume that one person controls one conversation and working folder. Team integrations can launch separate tasks, but do not let several people work with the same running agent. A live shared session must either queue requests or coordinate work that runs together.

We present Coagora, a coding agent in which several developers share one conversation and working folder. Each request receives a separate, labeled response. Model responses can run together, while completed conversation entries and conflicting file changes are saved one at a time. Changes to different files may proceed together.

In a controlled experiment, every extra second of the first user's task added about one second to the second user's wait when requests were queued or retried, but added no measurable wait when they ran together. With a live model, median wait fell from 38.2 to 10.8 seconds. The change gate prevented two Coagora tools from writing the same file at once in all 30 trials. Running requests together used 1.49× more input tokens in a three-question workload.

Labels and ordered writes do not ensure that the model follows the right request. Coagora can therefore hide other active requests and limit each request to approved files whose changes remain private until a task check passes. Across 60 live-model request pairs, all task files were correct; one response without the private view referred to the other task. Nineteen deliberately difficult cases confirmed that, when changes use Coagora's tools and paths remain stable, a request publishes only checked files from its approved list. This guarantee covers file changes, not response meaning or the quality of the task check.

[TODO after the first-use study: add one sentence reporting the central human finding from §6.]

---

## 1 Introduction

Coding agents now read repositories, edit files, run commands and tests, and open pull requests with limited supervision [1, 2]. Studies and telemetry show that they can complete substantial work, and that users frequently redirect them while they run [2, 3]. Yet the interaction model remains almost entirely single-player: one person owns the prompt, the context, and the controls.

Software development is collaborative. Several products now let teams mention an agent from Slack or Teams [6, 14–18], and users have requested Google-Docs-style access to a single coding-agent session [5]. The deployed messenger pattern is useful, but it is delegation rather than co-presence. A mention launches a cloud task from a captured thread, the agent works elsewhere, and a pull request returns later. Participants cannot usually intervene during execution, and published documentation does not provide a general rule for two people acting at once.

A live shared session keeps the conversation, files, and controls in one place. It also creates a coordination problem. A quick question can become stuck behind a long build if the system handles one request at a time. Separate sessions avoid that wait, but split the conversation and leave the team to merge both code and decisions.

Coagora explores a third option: **run model requests together, but coordinate shared changes**. Every message starts a separate response stream, called a *turn*. All turns use the same saved conversation and working folder. A turn identifier links each response, tool call, result, and cancel action to the person who sent the request. The model may work on several turns at once, but conflicting file changes run one at a time and completed conversation entries are saved as whole units.

This design applies familiar database and operating-system mechanisms below the model call [11]. It aims to keep the interface responsive without confusing the record of who requested what. It also reveals an important limit: a technically well-formed session can still contain a model response that follows the wrong person's request.

We study three technical questions and reserve one question for the planned user study:

- **RQ1 — Responsiveness.** How long does a new request wait when another request is already running?
- **RQ2 — Safe shared changes and cost.** Which changes must run one at a time, and what performance and token costs follow?
- **RQ3 — Correct ownership and request separation.** Does the session preserve its history, labels, queue, and resume behavior, and can one active request influence another?
- **RQ4 — First use.** What do pairs notice, infer, and do while sharing a live session? Section 6 reserves this question for the planned study.

All three request-handling policies run in the same system and use the same server, conversation manager, tools, and measurements. Controlled experiments test system behavior; live-model experiments test behavior that may depend on the model or serving system.

This paper contributes:

1. **A design space** that asks where shared state lives, how the system handles simultaneous requests, how it shows ownership, and when people can intervene.
2. **A working shared-session design** that keeps responses separate, saves conversation updates safely, coordinates file changes, queues users fairly, restores disconnected clients, and lets people cancel their own requests.
3. **Measurements of the design's benefits and limits,** including waiting time, token use, out-of-date conversation views, file conflicts, and enforcement of per-request file limits.

The planned first-use study will later add evidence about how people understand and manage these trade-offs. Until then, the paper makes no claim that running requests together improves team performance or long-term collaboration.

---

## 2 Related Work and Design Space

### 2.1 From individual agents to team access

Research on AI pair programming usually studies one developer with one assistant or agent. Copilot can increase output while reducing quality relative to human pairing [12], developers inspect AI suggestions differently from human suggestions [1], and understanding agent behavior remains a barrier to adoption [2]. In 6,000 real agent sessions, users interrupted or redirected the agent in 39% of available opportunities [3]. These findings establish that steering matters, but assume one person controls it.

Team access is emerging through messenger integrations. We reviewed public documentation for Claude Tag, Codex, GitHub Copilot, Devin, Cursor, OpenTag, and tools that run tasks in separate Git worktrees [6, 9, 10, 13–18]. We included systems whose documentation explained where the conversation and files live and what happens when another request arrives. The comparison covers these documented systems rather than every available product.

The five messenger integrations follow a similar path: mention, thread capture, cloud task, and pull-request link. Identity and persistence differ, but the live interaction boundary is similar. GitHub warns that a full thread may be stored in the resulting pull request [7], and indirect prompt injection through Slack AI shows that treating a channel as model context has security consequences [19]. Claude Tag describes a shared channel instance as multiplayer, but does not publish a protocol for simultaneous instructions [6].

### 2.2 Multiple people around one AI

The closest human-centered work comes from collaborative writing, group conversation, and education. Agents placed in a multi-user writing environment became both personal workspaces and shared team resources [20]. Group-conversation research has studied when, where, and how an AI participant should respond [21], while GroupMemBench shows that memory remains difficult in multi-party conversations [22].

In programming education, Daryanto et al. compared one-human/one-AI and two-human/one-AI configurations [23]. Peer visibility encouraged participants to examine AI suggestions more carefully. Their system was suggestion-based and the task was a one-off learning exercise, rather than a persistent autonomous agent with file and shell access. Work on agent-authored pull requests likewise studies collaboration around an agent's output after execution [24], not several people sharing the live execution itself. Public human-agent interaction data is sparse more generally [25].

These studies motivate questions of awareness, accountability, and control, but do not define how simultaneous requests should share one coding workspace.

### 2.3 Concurrency over shared agent state

Systems that run several AI agents already offer ways to coordinate shared files. CoAgent can undo a sequence of actions after a conflict [11]. STORM detects conflicting edits when they are written [26]. Worktree-based tools give each agent a separate branch and merge later [10]. DeLM lets agents share information but checks whether their output is supported [27]. These systems coordinate several agents working toward one goal; Coagora coordinates several people giving requests to one agent session.

Frameworks also differ in what they do when a conversation is busy. LangGraph can reject, queue, interrupt, or roll back a request [8]. OpenTag allows one active request per conversation and rejects another with an HTTP 409 response [9]. Schedulers such as Justitia divide model capacity among jobs [28]. Coagora instead asks how people who are present in the same session should share access to it.

Coagora uses established mechanisms: a request reads a stable copy of the conversation, completed entries are saved together, and conflicting file changes use locks [32–35]. Our contribution is to use these mechanisms without making one person's model response wait for another person's entire task.

### 2.4 Four design questions

We compare the surveyed systems along four questions:

1. **Location of shared state:** Does the conversation only launch work elsewhere, or does it show the running session itself?
2. **Handling of simultaneous requests:** Is a new request rejected, queued, combined, run separately for later merging, or run alongside the current request?
3. **Visible ownership:** Does the interface identify an owner for a pull request, task, message, or individual response?
4. **Opportunity to intervene:** Can a person act only before or after a task, throughout the session, or only on their own request?

| System | Where work lives | New request while busy | Visible owner | When a user can intervene |
|---|---|---|---|---|
| Claude Tag [6] | detached | undocumented for simultaneous input | task / organization | before or after |
| Codex Slack [14] | detached | task per mention | task | before or after |
| Copilot Slack [16] | detached | serial per channel by default | PR / requester | before or after |
| Devin Slack [17] | detached, synchronized | resume; serial within a session | task / mapped user | coarse thread reply |
| Cursor Slack [18] | detached | isolated VM per invocation | task | before or after |
| OpenTag [9] | live conversation | reject with 409 | message | unavailable while busy |
| Parallel worktree tools [10] | separate branch | run separately, then merge | branch | per agent |
| Coagora serial | live session | queue; insert at tool boundaries | response / requester | steer the session |
| **Coagora parallel** | **live session** | **run together; coordinate changes** | **response / requester** | **cancel own response** |

The documented messenger integrations mainly start separate tasks, label work at the task level, and allow intervention only before or after execution. Coagora instead studies one live conversation with simultaneous, individually labeled responses and per-request cancel controls. The table describes the reviewed documentation; it is not a claim about undocumented or future systems.

CSCW research explains why these choices matter. Collaborators need a shared understanding of the work [36], awareness of who is doing what [37, 38], and ways to divide and coordinate tasks [39]. Mixed-initiative systems must also decide when an AI should act or ask [40]. In a multi-user setting, the system must additionally decide whom to ask and who may stop an action. Section 7 uses these ideas to interpret the technical results, and Section 6 reserves their direct study with users.

---

## 3 How One Shared Session Works

### 3.1 One request, one labeled response

A session contains one saved conversation, one working folder, and all requests that are currently running. Participants connect to the same session; joining does not create a private copy.

Each accepted message starts a *turn*: one response and any tool use needed to produce it. A turn has a unique identifier that appears on its streamed text, tool calls, results, errors, and saved history. The interface can therefore show simultaneous responses as separate cards labeled with their requester.

A participant may cancel their own active turn but not another participant's. Cancellation keeps output already shown, frees the processing slot, and leaves other turns running. Coagora does not yet support broader group policies for overriding or approving another person's action.

[FIGURE TODO: Show two participants sending requests at the same time, separate labeled response streams, one shared conversation, and the change gate. Place a queued-request timeline beside it for comparison.]

### 3.2 A shared but sometimes out-of-date conversation

Whenever the model is called, it receives a stable copy of the saved conversation. It does not see partial output from responses that are still being generated. When a model step finishes, its response and tool results are saved together. The permanent history therefore never contains half of a response-tool pair.

This copy may be out of date. Another turn can finish after the copy was made, so a running turn may miss newer information. Two turns can also make individually reasonable changes that are wrong when combined. Coagora preserves a readable conversation history; it does not make the model's reasoning correct.

### 3.3 Coordinating changes to the working folder

Generating a response is slow but does not itself change files. File edits and commands are usually shorter, but can conflict. Coagora lets model generation proceed together and checks each operation that changes the working folder before it runs. We call this check the *change gate*.

| Two requested operations | Policy | Reason |
|---|---|---|
| Reads or writes on different known files | may run together | the files do not conflict |
| Operations on the same file | run one at a time | avoid overlapping changes |
| Shell commands, package operations, or deletion | use the folder alone | the affected files may be unknown |
| A tool made of smaller tools | check each actual file operation | holding the gate for the whole tool could block indefinitely |
| A new or unclassified operation that may change files | use the folder alone | choose the safe default |

Before comparing paths, Coagora resolves relative path components and symbolic links. A file with several hard-link names is treated as affecting the whole folder because those names cannot be matched safely by path alone. Waiting operations follow arrival order. This may delay an otherwise safe file edit behind an earlier shell command, but it prevents a stream of small edits from postponing that command forever.

The gate does not merge conflicting edits or undo them. Edits made through the gate run one after another, and a later edit may replace an earlier one. A program outside Coagora can also see a file halfway through a direct overwrite. Thus, coordinating Coagora's writers is different from making every read atomic or ensuring that the combined edits are logically correct.

### 3.4 Fair access, reconnection, and resume

By default, at most four turns run at once. Additional requests wait in arrival order, and each participant may have only one active turn. One person therefore cannot occupy every slot with a backlog. This queue cannot skip over a shell command that already has sole access to the working folder.

The server numbers every saved event. A browser that disconnects can request events after the last number it received. If that number is too old or belongs to an earlier server process, the browser receives a fresh session view instead. When an idle session is suspended and resumed, its history and working folder remain available.

### 3.5 What the system does and does not guarantee

The guarantees below rely on five conditions:

| Condition | Meaning |
|---|---|
| A1 | File changes pass through one Coagora process. Edits from an IDE or another process do not. |
| A2 | Every Coagora tool correctly states which files it may change. Unknown operations use the folder alone. |
| A3 | A file's resolved path does not change between the safety check and the operation. Symbolic links are resolved and detected hard links use the folder alone, but a concurrent rename can still violate this condition. |
| A4 | A shell command finishes before the tool call returns. A detached background process can escape the gate. |
| A5 | In enforced mode, the request supplies a complete list of allowed files and a content or test check, and all changes use supported Coagora file tools. Other state-changing tools are disabled. |

| System property | Status |
|---|---|
| Conflicting changes made through Coagora do not overlap | guaranteed under A1–A5 |
| Each approved file replaces its old version in one filesystem operation after the task check passes | guaranteed in enforced mode under A1–A5 |
| All files in one request appear together after a crash | not guaranteed |
| Saved conversation entries remain complete and correctly paired | guaranteed |
| Each response remains labeled with its requester | guaranteed |
| A participant can cancel their own response | guaranteed |
| A recently disconnected browser receives all missed events | guaranteed while those events remain stored |
| Every running response sees the newest conversation | not guaranteed |
| A request changes a task file outside its approved list through Coagora tools | prevented in enforced mode under A1–A5 |
| The model always follows the correct request | not guaranteed |
| Separate changes combine into a correct repository | not guaranteed |
| Protection from malicious participants | outside the scope of this system |

---

## 4 Implementation

Coagora is a Python coding agent with a terminal interface and a web interface for people on the same local network. One operating-system process owns the shared conversation and working folder. It uses a separate thread for each active turn.

Before a model call, the conversation manager makes a read-only copy of the saved history. Each turn has its own requester label and system instructions. An optional private view removes messages created by other turns that are still running. Once a turn finishes, its messages become visible to later model calls. A shared summary is hidden when the system cannot tell which turn its contents came from.

Enforced mode requires two items with the request: a list of files that may change and a check for the expected result. Coagora reserves those files for the turn and stores edits in private temporary copies. The turn can read its own temporary edits, but other turns cannot. After the model finishes, Coagora confirms that the task check passes and that nobody changed the original files in the meantime. It then replaces each approved file. If either check fails, it publishes none of the staged files.

The change gate receives the affected paths from each tool. It resolves symbolic links and lets an operation use the folder alone when a file has multiple hard-link names or the affected paths are unknown. Compound tools request access only when their inner file operation runs. Saved event logs use an additional lock because direct concurrent appends behaved differently across the evaluated filesystems.

The browser receives a continuous stream of numbered events and reconnects from the last event it saw. A shared write token allows requests and changes. An optional read-only token allows viewing the conversation but blocks all changes and any read that exposes hidden instructions or files. It does not keep one participant's information private from another participant.

The server records timestamps and identifiers for requests, tool use, waiting, errors, token use, task checks, and saved file changes; it does not record prompt or response text in the metrics log. Time to first token starts when the server first receives the user's request. For the reject-and-retry condition, this includes time spent retrying.

The controlled test setup uses the real server and replaces only the model with a predictable local test model. Each condition gets a fresh working folder and user directory. Experiments that depend on natural model behavior or serving load use a live on-premise model. Appendix A links each result to its data and reproduction script.

---

## 5 Technical Evaluation

We evaluate how the system behaves, how long users wait, and what the safety checks cover. Following guidance for HCI systems and toolkits [41], controlled experiments test mechanisms and live-model experiments test behavior that depends on deployment. The planned user study addresses collaboration outcomes.

Comparisons use the same program and change only the policy being tested. A complete trial, rather than each event observed inside it, is the unit of analysis. We report exact 95% confidence intervals for yes/no outcomes and exact paired tests when the same trial compares two policies. High-frequency observations inside one trial are descriptive. Checks with fully scripted expected results are treated as software verification, not estimates of real-world frequency.

### 5.1 RQ1: How long does a new request wait?

#### Waiting behind a long task

User A starts a task lasting 2, 6, 15, or 30 seconds. User B asks a one-line question 0.5 seconds later. We compare three policies: queue B behind A, reject B until retry succeeds, or run both requests together. Of 240 trials, 236 produced a first response token that could be linked to B's request.

| Policy | A: 2 s | A: 6 s | A: 15 s | A: 30 s | Added wait per second of A |
|---|---:|---:|---:|---:|---:|
| Serial | 2.08 s | 6.16 s | 15.34 s | 30.86 s | 1.028 |
| Reject + retry | 2.31 s | 6.34 s | 15.42 s | 31.05 s | 1.027 |
| **Parallel** | **0.292 s** | **0.291 s** | **0.291 s** | **0.235 s** | **−0.002** |

The 30-second condition ran in a second session with 56 ms less setup time. A control run reproduced this constant shift, which does not affect comparisons within a condition or the relationship between A's task length and B's wait. Four trials without an identifiable first token are listed in the raw data and excluded here.

When requests are queued or repeatedly retried, each extra second of A's task adds about one second to B's wait. Retrying also adds delay: for a 15-second task, retry intervals of 250 and 1,000 ms made B wait 172 and 794 ms longer than the queue and required 61 and 16 attempts. When both requests run together, B's wait does not grow with A's task length.

The queued policy can insert B's question when A reaches a tool call. B therefore waits for the current uninterrupted part of A's task, not necessarily the entire task. We fixed A's total task at 15 seconds and divided it into different numbers of tool calls:

| Tool calls in A | Longest uninterrupted part | B inserted before A finished | Time until B entered a model request |
|---:|---:|---:|---:|
| 1 | 15.0 s | 0/10 | 15.10 s |
| 2 | 7.5 s | 10/10 | 7.30 s |
| 4 | 3.75 s | 10/10 | 3.39 s |
| 8 | 1.875 s | 10/10 | 1.44 s |

This measure stops when B's question reaches the model, before B receives an answer. Even with eight tool calls, it is 4.9× the parallel time to first token for the same task length. Queuing works better when A reaches frequent tool boundaries and approaches whole-task waiting during a long generation, build, or test.

With the queue limit raised so that everyone could start, increasing the total number of users from 2 to 4 and 8 increased a questioner's time to first token from 236 to 265 and 328 ms. Seven simultaneous questioners added 39%, not sevenfold. This remaining delay comes from shared computing resources rather than the request policy.

#### Live model and token use

With a live on-premise model, B asked a question two seconds after A began a long response. Across twelve repetitions, B's median time to first token was **38.2 seconds when queued and 10.8 seconds when both requests ran together**. Their observed ranges and confidence intervals computed by resampling did not overlap. Under heavier model-server load, the corresponding medians were 43.6 and 12.3 seconds.

The model server still limits the benefit. Without Coagora, eight simultaneous responses took 2.68× as long as one, although the server produced about three times as much total output per second. Coagora can send B's request promptly, but cannot remove competition inside the model server.

Running requests together also makes more model calls. For the same three questions, the queued policy combined work into two calls and used 15.6K input tokens; the parallel policy used three calls and 23.2K tokens, or **1.49× as many**. After five earlier turns, the ratio was 1.47×. Faster access therefore comes with higher input-token use.

### 5.2 RQ2: Preventing conflicting file changes

#### What the change gate prevents

This experiment forces two Coagora tools to overwrite the same file. Each policy runs 30 times in a fresh process and folder, and policy order is randomized. Every 2 ms, a separate program outside Coagora reads the file. We measure whether the two tools write at the same time, whether the outside reader sees content mixed from both writers, and whether the final file is valid.

| Outcome across 30 trials | No gate | One lock for the folder | Lock only conflicting files |
|---|---:|---:|---:|
| Coagora tools wrote at the same time | 30/30 | 0/30 | 0/30 |
| Outside reader saw content mixed from both writers | 27/30, 95% CI [73.5%, 97.9%] | 0/30, [0%, 11.6%] | 0/30, [0%, 11.6%] |
| Final file mixed content from both writers | 3/30 | 0/30 | 0/30 |
| Outside reader saw an empty or partial file | 29/30 | 30/30 | 29/30 |

Both locking policies eliminated mixed-writer content in these trials (paired exact p = 1.49 × 10⁻⁸ versus no gate). Reading every 1, 5, or 10 ms produced the same pattern: the unlocked condition exposed mixed content in 24/30, 24/30, and 13/30 trials, while neither lock did. However, all policies sometimes exposed an empty or incomplete file during a direct overwrite. The gate coordinates Coagora's own writers; it does not protect outside readers from seeing an update in progress.

#### When one lock for the whole folder becomes expensive

We next vary the percentage of each turn spent changing files rather than generating a response. With two turns, one turn can generate text while the other changes files until file-changing work exceeds half of each turn. Beyond that point, forcing all file changes through one folder-wide lock reduces how much work can overlap. The table reports the median of five trials per cell. A value of 1 means that no useful work overlaps; 2 means that both turns overlap completely. The last column divides conflict-only locking by folder-wide locking.

| Time spent changing files | Files used by the turns | Overlap with one folder lock | Overlap when only conflicts lock | Ratio |
|---:|---|---:|---:|---:|
| 25% | disjoint | 1.987 | 1.992 | 1.00× |
| 50% | disjoint | 1.974 | 1.983 | 1.01× |
| 75% | disjoint | 1.662 | 1.977 | 1.19× |
| 90% | disjoint | 1.368 | 1.983 | 1.45× |
| 90% | same path | 1.369 | 1.368 | 1.00× |

When both turns use the same file, both policies must wait and perform similarly. When they use different files, conflict-only locking remains near the maximum possible overlap. Its advantage becomes substantial only when file-changing work occupies most of a turn.

Live model turns spent little time inside file tools: eight writes over about four minutes used 10⁻⁵ of the total turn time and waited a median 0.1 ms for a lock. Shell commands mattered more because they receive the folder alone. Three one-second commands used 2.5% of the turn and caused no measurable wait; three five-second commands used 9.4% and caused about 4.1 seconds of waiting. In these workloads, shell commands—not ordinary file edits—were the main source of lock delay.

### 5.3 RQ3a: Does the session remain internally consistent?

The following experiments check basic properties needed for a usable shared session.

| Property | Test | Result |
|---|---|---|
| Reconnection | 90 turns; uninterrupted stream versus 11 disconnect/reconnect cycles | all 180 events returned in the same order with no missing or duplicate event |
| Short conversation summaries | 3 users × 30 rounds; each summary took 800 ms | all 90 questions remained; all 4 summaries were saved while 42 new events arrived |
| Live-model summaries | 3 users × 8 rounds; summaries took 56–123 s | all 24 questions remained; 2/5 summaries were saved and 3 were discarded because newer messages had arrived |
| Requester labels | 4 users × 25 rounds | all 100 questions linked to exactly one response; none was missing or duplicated |
| Conversation freshness | live model with 3 users; test model with 4 users | live: 75/92 model calls missed newer entries, median 2 and maximum 10; test model: 498/500, median 3–4 and maximum 5; queued controls missed none |
| Fair access | one user sends a backlog while others ask short questions | with the per-user queue: 4.8 ms median start; without it: 151.2 s; never more than one active turn per user |
| Suspend and resume | 204 turns across three cycles | all 204 questions remained, identifiers stayed unique, and stored state remained bounded; a smaller live-model test retained 27/27 |

Conversation summaries run without stopping new turns. If messages arrive before a long summary finishes, Coagora discards that summary instead of saving an out-of-date replacement. Reconnection is exact while the missed events remain in memory; an older browser receives a complete fresh view.

The per-user queue prevents one person's backlog from filling every processing slot, but it cannot bypass a shell command that already has the working folder. In a direct test, another user's file edit waited roughly as long as one shell command at the 95th percentile: 996 ms for a one-second command and 4,992 ms for a five-second command. Arrival-order processing prevents indefinite postponement but preserves this wait.

Running model calls without interruption means that they often use a valid but older conversation. Long live-model responses missed more new entries than the controlled test model. The current interface does not show users how old a turn's conversation view is.

### 5.4 RQ3b: Keeping one request from changing another request's files

An owner label records who sent a request, but it does not determine which request the model follows. Coagora uses three increasingly strong protections. First, the model instructions name the current request. Second, a private conversation view hides messages from other active requests. Third, enforced mode limits the turn to an approved list of files and publishes changes only after a task-specific check passes.

The live experiment used Qwen3.6-27B-MLX-8bit on an on-premise server. Two requests ran together: one created parser files and the other wrote command-line documentation. Each request named two allowed files and their expected contents. We tested the three protections in 20 groups of trials, using a fresh folder each time and rotating their order. Temperature, top-p, and seed were not fixed, so the results describe this server and collection date.

The pair of simultaneous requests is the unit of analysis. Automated checks record whether either request tried to use or actually changed the other's files, whether both tasks and the final folder matched the expected contents, whether a response mentioned the other task's completion label, total time, and input tokens. Enforced-mode file changes are read from the system's audit log and compared with the approved file lists.

| Outcome across 20 request pairs | Request named in instructions | Other active request hidden | Allowed files and task check enforced |
|---|---:|---:|---:|
| Tried to use the other task's file | 0/20 (0–16.84%) | 0/20 (0–16.84%) | 0/20 (0–16.84%) |
| Changed the other task's file | 0/20 (0–16.84%) | 0/20 (0–16.84%) | 0/20 (0–16.84%) |
| Both tasks matched expected contents | 20/20 (83.16–100%) | 20/20 (83.16–100%) | 20/20 (83.16–100%) |
| Final folder matched all expected contents | 20/20 (83.16–100%) | 20/20 (83.16–100%) | 20/20 (83.16–100%) |
| A response mentioned the other task's completion label | 1/20 (0.13–24.87%) | 0/20 (0–16.84%) | 0/20 (0–16.84%) |
| Median time of the slower request | 59.5 s | 52.1 s | 59.5 s |
| Median input tokens per request pair | 33,508 | 32,687 | 33,177 |

All three configurations produced the same file results. The single reference to the other task is too little evidence to distinguish them statistically (paired exact test, p = 1.0). Hiding the other active request used 2.5% fewer median input tokens and reduced the median time of the slower request by 12.4%. Enforced mode used 1.0% fewer tokens and had the same median time as instructions alone. These small differences describe this server rather than a general speed advantage. Enforced mode checked and saved the files from all 40 requests without a failed check.

Because the live model did not try to misuse a file, we separately tested 19 deliberately difficult cases. These included paths outside the approved list, tools with unknown changes, failed task checks, files changed by another process, multiple names for the same file, and two requests for the same file. All produced the expected result: unapproved or failed changes did not reach the shared folder, and two claims on the same file did not overlap.

Under the conditions in §3.5, enforced mode publishes changes only to approved files and only after the supplied task check passes. It does not ensure that the model's explanation is correct, that the task check fully captures the user's intent, or that all files appear together after a crash.

### 5.5 Limits of the evidence

The predictable test model removes random variation but cannot represent natural model behavior or competition inside a model server. We use it to test system mechanisms and use live models for response time, request separation, token use, conversation summaries, fairness, and suspend/resume behavior. The live results come from one on-premise server, one model for the three-protection experiment, one host computer, and mostly two or three simulated users.

The tasks are designed to expose conflicts and limits. Their rates describe these tests, not the frequency of problems in production teams.

The repository has 3,617 passing tests and 35 tests skipped because of environment requirements, including 53 browser tests. Independent replication is still needed.

The technical evaluation does not measure collaboration outcomes. Section 6 addresses awareness, coordination, control, and deployment fit in the planned first-use study.

---

## 6 RQ4: First Use of a Shared Session [TODO: study pending]

The experiments above show what the system did, but not what people understood. Logs can show that one person waited, that two responses ran together, or that a request changed another task's file. They cannot show whether a participant noticed, how they explained the event, or what they did next. The planned first-use study therefore examines five questions: Do participants know what their partner is doing (M1)? Do they notice recorded events (M2)? What do they think caused them (M3)? What rules do pairs develop for working together (M4)? In what settings would they use the system (M5)?

**Participants and ethics.** The target sample is four to six pairs of peer developers, excluding the authors and people with a direct conflict of interest. Each pair completes one approximately 85-minute session. Participants provide written consent before screen and audio recording begins, may stop at any time, and approve any quotation before publication. Raw history text is deleted after analysis; only coded data and approved, anonymized quotations are retained.

[TODO after recruitment: report the final number of pairs and participants, relevant experience, recruitment route, compensation, pilot handling, exclusions or dropouts, and the institutional ethics/IRB determination. If fewer than four main-study pairs complete the protocol, relabel this section as pilot observations rather than a study result.]

**Design and tasks.** Each pair uses both policies for 20 minutes: requests wait in a queue in one condition and run together in the other. We vary which condition and task pair comes first across teams. In both conditions, the model instructions identify the current request. Two participants use separate laptops to connect to the same local-network session and prepared TaskBook repository. Each participant initially sees only their own task card, allowing us to study what they learn about their partner through the shared session.

After both main conditions, every pair completes an eight-minute diagnostic condition, Module C. Requests run together, but the model instructions no longer identify the current request. This condition tests whether participants notice the cross-request behavior measured in §5.4. It always appears last, is disclosed during consent, and is explained afterward. We use it only to study detection, not to estimate how often the problem occurs or to compare it causally with the main conditions.

**Procedure and measures.** Both participants send their first requests at the same time. At minutes 8 and 16 of each main condition, the facilitator pauses them for ten seconds and asks, “What is your partner doing right now?” The same question is asked halfway through Module C. Two researchers independently compare each answer with the partner's recorded activity. They score it 2 when both the task area and current action are correct, 1 when the area is correct but the action is vague or wrong, and 0 when the answer is wrong or “do not know.” They discuss disagreements until they agree.

After each main condition, participants rate five items from 1 to 7: response speed, awareness of their partner, whether the agent appeared to follow the wrong request, sense of control, and willingness to collaborate with the system. They also answer one open question. In the final interview, the facilitator selects three to five recorded events, such as a request changing the other task's file, a long queue, a cancel or retry, a conversation summary, or a period when both requests were active. Participants say whether they remember each event, what they think caused it, and what they did. We retain reports of events absent from the logs as false alarms. The interview also covers rules the pair developed, who should be allowed to cancel work, what participants did while waiting, and conditions for workplace use.

**Analysis.** Logs provide the reference for what happened and help select interview events. The study does not re-estimate waiting time or file-conflict rates, which §5 measures with more trials. Because the sample is small and qualitative, we will not use hypothesis tests. We will report awareness scores, remembered and missed events, explanations of their causes, pair-level working rules, and whether use was acceptable, conditional, or unacceptable. Questionnaire results will be descriptive. We will report counterexamples as well as common patterns.

### 6.1 Study completion and data quality

| Item | Result to report after the study |
|---|---|
| Main-study sample | [TODO: pairs, participants, completed sessions] |
| Condition exposure | [TODO: counts starting with queued / together; completed A, B, and C sessions] |
| Collected evidence | [TODO: valid awareness questions, questionnaire responses, interview events, approved quotations] |
| Missing or excluded data | [TODO: count, reason, and handling; write “none” if none] |
| Coding agreement | [TODO: initial agreement for M1 and M3, disagreements, and consensus procedure actually used] |

### 6.2 M1: Awareness of the partner's work

| Condition | Valid probes | Score 0 | Score 1 | Score 2 | “Do not know” |
|---|---:|---:|---:|---:|---:|
| Requests queued | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |
| Requests run together | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |
| Module C (together, current request not identified) | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |

[TODO: Write one short results paragraph describing the distribution across teams and conditions. State what participants could and could not identify; do not infer an effect for the wider population. Add one counterexample if a pair behaved differently from the dominant pattern.]

### 6.3 M2 and M3: Remembered events and their perceived causes

| Recorded event type | Events discussed | Remembered | Missed | False alarms | Perceived cause |
|---|---:|---:|---:|---:|---|
| One request changed the other task's file | [TODO] | [TODO] | [TODO] | [TODO] | [TODO: agent / system / partner / self / other] |
| Long wait in the queue | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |
| Cancel, retry, conversation summary, or simultaneous work | [TODO] | [TODO] | [TODO] | [TODO] | [TODO] |
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

**Scope of the claims.** This study can show what participants noticed, how they interpreted events, the rules they formed, and the settings in which they would or would not use a shared agent. It cannot establish that running requests together improves team performance, estimate waiting or cross-request error rates, or predict long-term adoption.

---

## 7 Discussion

### 7.1 One shared session changes the coordination problem

When an agent runs tasks separately, people must track where each task went and combine the results later. A live session moves some of this coordination into the interface: a queue decides when requests start, the change gate coordinates file operations, and labels show who sent each request. People must still divide work, watch simultaneous responses, decide when to cancel, and resolve requests that overlap in meaning [39].

A saved conversation does not give every participant the same understanding [36]. Someone who joins late can read the same history but may not know why decisions were made. Ordered, labeled records could support decision summaries, per-file histories, or filters by requester, but we do not evaluate those designs.

Control also becomes a group issue. Letting people cancel only their own request is predictable, but may be insufficient when one request can delete or replace shared files. Teams may need role-based overrides, visible consent, or approval from several members. The request identifier gives such policies a clear object to control.

[TODO after §6: connect observed awareness, interrupt expectations, and coordination norms to this section without generalizing beyond first use.]

### 7.2 Design implications

The evaluation suggests four principles for shared-agent interfaces.

1. **Show who asked and what the model acted on.** A correct requester label does not prove that the model followed that request. The interface should also show which files and commands the response affected.
2. **Show when a response is using older information.** A response may have started before newer messages arrived. An age indicator or “new activity available” notice could help users decide whether to continue, restart, or cancel it.
3. **Check changes, not only model instructions.** Instructions and a hidden conversation can influence the model but cannot guarantee its behavior. Systems should show and approve the files a request may change, keep edits private until checked, and ask the requester when the needed file list expands.
4. **Explain why a request is waiting.** A request may wait for a model slot, a file-changing operation, or the model server. These causes have different remedies; a single “busy” indicator hides useful information.

These are implications derived from measured system behavior. Whether users understand or value them remains for the first-use and later field studies.

### 7.3 Trust and deployment boundary

Coagora assumes mutually trusting collaborators who already share a repository. Every participant's message enters the shared conversation, so one person's text may influence another response. There is no private conversation for each participant and no defense against a malicious collaborator. The read-only token separates watching from acting, but not one participant's information from another's.

Restricting file changes and restricting model input solve different problems. Enforced mode controls supported Coagora tools, not another program or hostile code, and a folder boundary is not a security sandbox. A sandbox, however, would still allow the model to follow the wrong participant's request inside that folder. Use with untrusted participants requires both operating-system isolation and rules that distinguish participants' permissions [30, 31].

### 7.4 Limitations

- **Short, synthetic workloads.** The experiments expose mechanisms and boundaries rather than the frequency of events in production work.
- **Narrow deployment environment.** Live results come from one model server and one host computer; the file-restriction experiment uses one model. Another model or server may produce different response times and behavior.
- **Token cost.** Running requests together used 1.49× the input tokens in the measured workload because it made three model calls instead of two.
- **Conversation growth.** A shared history grows faster with more participants. New requests continue while it is summarized, but 3 of 5 long live-model summaries became outdated before they could be saved.
- **File-change boundary.** Validation covers only the supplied task check and approved files. The current API requires both with the initial request and does not yet support a model proposing additional files for user approval. Separately valid changes may still conflict in meaning.
- **Derived indexes.** Enforced mode does not update search indexes outside the approved file list; rebuilding them requires a separate authorized operation.
- **Processes outside Coagora.** Detached background processes bypass the change gate, and shell commands still require exclusive access to the folder.
- **Single process and directory-level isolation.** Sessions do not span machines and are not containers.
- **Limited reconnection history.** Exact recovery covers only events still held by the server; older clients receive a complete fresh view.
- **Control policy.** Participants can cancel an active request, but cannot yet redirect it, undo shared changes, or require group approval.
- **No human outcome yet.** The planned study can characterize first use but will not establish long-term adoption or team productivity.

---

## 8 Conclusion

Sharing a coding agent requires more than connecting several browsers to one chat. The system must decide when each request starts, show who requested each action, coordinate changes to shared files, and explain what information a running response has seen.

Coagora lets model responses run together while saving conversation entries and conflicting file changes in a controlled order. This removes the wait behind another person's entire model response. The measured costs are additional model calls, responses that sometimes use older conversation history, and waiting during long shell commands.

Correct labels and orderly files do not guarantee that the model follows the intended request. Coagora therefore adds a private view for active requests and an enforced mode that accepts changes only to approved files after a task check passes. Under the stated conditions, this prevents a request from publishing changes outside its approved file list. It does not guarantee that the model's explanation or the task check captures the user's intent. Shared-agent interfaces should make requester identity, conversation age, approved files, and resulting changes visible. The planned first-use study will examine whether people understand and can use these controls.

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

## Appendix A: Reproduction materials (draft)

The repository contains the system, a predictable test model, original JSONL results, and summary files. Controlled scripts use the real server with the test model; live-model scripts require an OpenAI-compatible model server. Each trial uses a fresh temporary working folder and user directory.

| Script | Evidence |
|---|---|
| e2_hol.py, e2b_injection.py, e2c_retry.py, e2d_nscale.py | §5.1 waiting, tool-call opportunities, retry, and number of users |
| e1_ablation.py | §5.2 independent-run writer ordering, external visibility, final state, and sampling sensitivity |
| p2_grid.py, p2_scope.py, p2_scope_real.py, p2_shell_real.py | §5.2 file-changing time, lock scope, and live-model behavior |
| n4_replay.py | §5.3 reconnection |
| n1_compaction.py | §5.3 controlled and live-model conversation summaries |
| p1_isolation_real.py, p1_adversarial.py | §5.4 three protections and deliberately difficult file-change cases |
| n5_staleness.py | §5.3 age of the conversation seen by a response |
| p4_fairness.py, p4b_mixed_fairness.py | §5.3 fair request access and waiting for file changes |
| p7_lifecycle.py | §5.3 suspend and resume |
| p6_real_llm.py, p6b_provider_concurrency.py | §5.1 live ranking, tokens, and provider concurrency |
| stats_recompute.py | Statistical recomputation from committed raw files |
| verify_paper_claims.py | Re-derivation of quoted numerical claims |

[NOTE: Replace repository paths with anonymous archival links for double-blind review. The main paper must remain understandable without the artifact.]
