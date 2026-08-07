# Reviewer Simulation — 09-full-paper-draft.md (v0.8)

> 2026-08-07. 실제 심사 상황을 가정한 리뷰어 시뮬레이션. 대상: `09-full-paper-draft.md` v0.8 (2026-08-06).
> 가정 벤처: CSCW/UIST 계열 시스템 트랙 또는 ICSE/FSE 계열 (논문이 시스템 평가 + CSCW 이론을 모두 걸치고 있어 양쪽 관점을 모두 반영).

## 한국어 요약 (TL;DR)

- **총평: Major Revision (경계선 Weak Accept).** 문제 설정과 within-system 비교의 내적 타당성은 강하지만, (1) 헤드라인 수치가 mock 기반이라 "구성상 당연한" 결과에 가깝고, (2) 공유 트랜스크립트로 인한 의미론적 혼선(38% worst case, 실모델에서 남의 파일을 쓰는 사건 1/12)이 시스템의 핵심 전제를 흔드는데 완화책이 없으며, (3) 인간 평가가 전무하다는 세 가지가 크다.
- **가장 아픈 지적 (W2/Q1):** §6.1은 serial에서 B가 A의 턴 전체(L)를 기다린다고 하는데, §6.10은 serial이 mid-run injection으로 대기 질문을 실행 중 턴에 접붙인다(3질문 2호출)고 한다. injection이 있다면 B의 답이 A 턴 안에서 나올 수 있는데 왜 §6.1의 serial TTFT가 L 전체인가? 두 절 사이 정합성 해명이 필요하다.
- **누락된 관련 연구:** DB 동시성 제어 고전(스냅샷 격리, 락 granularity/intention lock, Gray & Reuter)을 전혀 인용하지 않음. 기여의 상당 부분이 고전 기법의 재적용이므로 반드시 위치 지정 필요.
- **위협 모델 부재:** 공유 컨텍스트는 사용자 간 프롬프트 주입 표면인데(§6.7의 혼선이 그 증거), 악의적 참가자 분석이 없다.
- **고칠 수 있는 것들:** 위 정합성 해명, DB 문헌 인용, abstract의 8.2% 표기 완화, reject 베이스라인의 retry-interval 민감도 1셀 추가, 통계 처리(분산/CI/회귀 적합 정보) 보강 — 대부분 major revision 한 사이클에서 해결 가능한 수준.

---

## Review (as submitted to the PC)

**Paper:** Coagora: Multiplayer Coding Agents via Concurrency Contracts for Synchronous Multi-User Sharing of a Single LLM Agent Session

**Recommendation:** Major Revision (would be Weak Accept if W1–W3 are convincingly addressed)

**Reviewer expertise:** 4/5 (systems for LLM serving and agents; some CSCW background)

**Scores (indicative):** Novelty 4/5 · Technical soundness 3/5 · Evaluation 3/5 · Presentation 3.5/5 · Reproducibility 5/5

### Summary

The paper addresses synchronous multi-user sharing of a single LLM coding-agent session. It (i) proposes a four-axis design space (state locus, concurrency contract, attribution unit, intervention point) locating nine deployed/prototype systems and identifying an empty coordinate; (ii) proposes the contract "parallel inference, serialized side effects" — concurrent turns infer over snapshots of one shared context while all side effects pass through a conflict-scoped hierarchical lock; (iii) implements the contract as a switch inside Coagora, an existing shared-session agent whose production default is the serial contract, enabling a within-system comparison of serial, reject-and-retry, and parallel contracts on one binary. Headline results: second-user TTFT slope vs. first-user task length is ~0.00 (parallel) vs. ~1.03 (serial, reject); a lock ablation reduces torn same-file writes from 8.2% to 0%; five further experiments cover incremental replay, concurrent compaction, structural attribution, per-user fairness, and lifecycle durability; a live-model spot check confirms ranking and prices parallelism at 1.49× input tokens. The discussion derives four open problems from CSCW theory.

### Strengths

- **S1. Timely, well-motivated problem with a genuinely empty coordinate.** The survey of the messenger-integration family (§2.2) is precise and fair — the authors characterize its limits as properties of the design-space coordinates rather than vendor failures, and they resist strawmanning (acknowledging autonomous iteration and Devin's session persistence).
- **S2. Unusually strong internal validity for the comparison.** The serial baseline is the system's own shipped production path; all three contracts are the same binary behind a switch (`--concurrency-contract`, `--lock-scope`, `--per-user-gate`). This is the right way to run a contract comparison and is rare in this literature.
- **S3. Exemplary reproducibility.** Deterministic, credential-free harness; committed raw JSONL per figure; explicit per-script artifact table (Appendix A); isolated `HOME` per condition. The fixed-overhead drift note (§6 Setup) with a control re-measurement is the kind of hygiene most papers skip.
- **S4. Honest negative/failure reporting.** The compaction-starvation bug (§6.6), the failed write-count axis of the §6.3 grid, the WSL append-corruption probe (§5), and the cross-user file-write incident (§6.4) are all reported rather than hidden. This materially increases my trust in the numbers.
- **S5. The two-experiment structure of §6.4** (controlled sweep locates the law; live run locates the operating point; each stated as unable to replace the other) is methodologically clean, as is the §6.2 justification for bypassing the model layer.

### Major weaknesses

- **W1. The headline result is close to true-by-construction, and the paper's framing does not fully own this.** With a mock LLM whose latency is scripted, "parallel inference has TTFT independent of the other turn's length" is a correctness check of the implementation, not a finding about the world; slope 1.03 vs 0.00 could be predicted from the architecture diagram. The empirically contentful results are elsewhere: the live-model gap (7.0 s vs the 0.29 s harness floor, §6.10 — i.e., real-world independence is bounded by serving-layer concurrency), the compaction/attribution/replay properties, and the 1.49× token accounting. I would accept the mock grid as a *validation* experiment, but the abstract and §6.1 currently present it as the primary empirical contribution. Reframe, and promote §6.10 (currently n=5/n=3 "spot checks") to a first-class experiment with enough repetitions to report variance.

- **W2. Apparent inconsistency between §6.1 and §6.10 in what the serial contract can do.** §6.10 states the serial contract "quietly batches: its mid-run injection folds a queued question into the running turn (3 questions, 2 calls)." If serial can inject a queued question into a running turn, then B's answer can in principle be produced *within* A's turn, and B's TTFT under serial need not be ≈ L. Yet §6.1 measures serial TTFT ≈ L at every level. Either (a) injection exists but the injected answer still only streams after the running turn's current step/turn completes, (b) injection is disabled in the §6.1 arm, or (c) the mock cannot exercise injection. Whichever holds, the paper must say so explicitly, because as written the strongest baseline configuration of the authors' own system appears not to be the one measured in the headline table. This is the single most important clarification for the rebuttal.

- **W3. Semantic confusion is treated as an "incidental observation" when it strikes at the core premise.** The system's raison d'être is one shared context. §6.7 shows an adversarial model misdirects 38% of answers; §6.6 shows 4/90 under light load with a non-adversarial mock; §6.4 shows a *live* model executing the other user's task — writing their files — in 1 of 12 runs, under instructions the authors admit are near-worst-case but hardly exotic (two users writing different files). Structural attribution correctly records the mishap; it does not prevent it. For a coding agent, wrong-file side effects are a correctness violation from the user's standpoint, regardless of the lock's physical guarantees. The paper needs at least: (i) a serious discussion of mitigations (per-turn instruction scoping in the prompt, addressing/turn-tagging conventions, post-hoc effect-attribution checks) and ideally a measurement of one; (ii) elevation from "incidental" to a named limitation with its own experiment. Related: this is also a **security** surface — see W5.

- **W4. Missing engagement with the database concurrency-control literature.** Snapshot read + atomic completion-order commit is snapshot isolation; the conflict-scoped hierarchical lock with a compatibility matrix and strict-FIFO admission is textbook lock granularity (Gray et al.'s granular/intention locks; two-phase commit hygiene; Bernstein/Goodman). "Last-wins by construction of order" is a known anomaly class, not a resolution. The contract's novelty is the *placement* of these mechanisms (effects locked, inference never), which is a real contribution — but the paper cites only the agent-systems optimistic family (§2.4) and never the fifty-year-old literature it is transplanting. A systems reviewer will read the omission as either unawareness or overclaiming. Cite and position.

- **W5. No threat model for the multi-user setting.** The paper cites the Slack AI indirect-injection case (AML.CS0035) against the *messenger* family, but its own design shares one transcript across principals: any participant's message is context for every other participant's turns, which is precisely a cross-user prompt-injection channel — and §6.7's mechanism demonstrates the channel is live. The read-only token and constant-time comparison are mentioned, but there is no analysis of a malicious or careless *writing* participant. The authors' own reference [30] (multi-user permission/privacy policy) is dismissed as a complement; at minimum the paper should state which classes of cross-principal attack the contract does and does not address. Limitation (7) ("isolation depth") gestures at this but conflates filesystem isolation with prompt-level isolation.

- **W6. Scale and external validity.** All experiments use 2–5 users, a concurrency cap of 3–4, short histories, and synthetic workloads (admitted, [TODO: S1]). Claims are phrased about "teams," but nothing here tests N beyond a handful, long-horizon context pressure (admitted unmeasured in Limitation 2), or the token premium's growth with history length (admitted). The reject baseline's "tax" is a function of the chosen 250 ms client retry interval; a sensitivity cell (e.g., 1 s retry) would cost little and preempt the objection. LangGraph's *enqueue* and *interrupt* strategies — arguably stronger members of the double-texting family than reject — are named in §2.4 but not measured (enqueue ≈ the serial arm, which should be stated if it is the intended mapping).

### Questions for the authors (rebuttal)

1. **Q1 (W2).** Reconcile §6.1 and §6.10: can serial's mid-run injection answer B during A's turn, and if so why does serial TTFT ≈ L in the grid? Was injection active in the §6.1 serial arm?
2. **Q2 (W3).** Have you tried any prompt-level mitigation for cross-turn semantic confusion (e.g., prepending "you are serving turn t_i for user U; other users' concurrent requests are visible but not yours to execute")? Even a negative result would substantially strengthen §6.7.
3. **Q3 (W1).** For §6.10, what is the variance across the 5 ranking repetitions, and what serving stack/concurrency limit was the on-premise endpoint running? "Independence from A's existence is bounded by the provider's concurrency" needs at least the provider's concurrency stated.
4. **Q4.** Bounded staleness (§4.3, Limitation 5) is disclosed but never measured: in the live runs, how often did a turn's snapshot omit another turn's committed effects, and did any answer visibly rest on stale state?
5. **Q5.** The fairness experiment (§6.8) ablates the gate under one flooding pattern. Does strict-FIFO-no-overtaking at the *effect* layer (§4.4) interact with the *admission* fair queue under mixed workloads — can a shell-heavy user degrade file-heavy users' effect latency despite admission fairness?
6. **Q6.** Table 1 rows for Coagora list D4 "during, session-scoped" (serial) vs "during, turn-scoped" (parallel), but Limitation (8) says steer-with-interrupt is unsupported on concurrent turns — pure cancellation only. So the parallel contract's intervention is *narrower* in kind than the serial mode's mid-run injection? The design space should record this honestly.
7. **Q7.** The claim "first user-level fairness mechanism at turn granularity" (§2.4) — first among what population? Multi-tenant serving fairness (per-client token buckets, per-user rate limits) is standard in API gateways; delimit the claim.
8. **Q8.** For §6.2, the 8.2% is under forced continuous overlap. The abstract's phrasing ("8.2% of sampled concurrent same-file writes") will be read as a field rate. Will you re-phrase the abstract to mark it as a forced-overlap ablation?

### Detailed comments by section

- **Abstract.** Far too long (~500 words) and tries to enumerate every experiment. Cut the property list; keep slope, integrity, token premium, and the research agenda. Mark 8.2% as an ablation (Q8).
- **§1.** Strong. "The choice is false" framing is effective. The footnote-like density of the messenger critique could move to §2.2.
- **§3.** The design space is useful but informally derived; say how the axes were obtained (bottom-up from the survey?) and whether any surveyed system resists classification. "undefined" for Claude Tag's D2 is fair but should be visually distinguished from a positive value. Nine systems, two of which are your own rows, is seven external points for a 4-axis space — acknowledge sparsity.
- **§4.3.** "Staleness is a freshness limitation, never a consistency violation" — this is the definition of snapshot isolation minus write-skew analysis; note that write-skew *is* possible at the semantic level (two turns each read the same snapshot, write disjoint files, jointly violate an invariant), which your last-wins discussion doesn't cover. Cite DB literature (W4).
- **§4.4.** The "not orderable → no lock" narrowing is well argued, and the deadlock reasoning for composite tools is a nice implementation insight. But "a shell's file footprint is statically unknowable" concedes that any turn issuing shell commands serializes the effect layer globally — for *coding* agents, shell (build/test) is the dominant effect. This tension with §6.3's "the common case sits far from the boundary" deserves one honest paragraph: the 10⁻⁵ operating point of §6.4 comes from a file-write workload; a test-heavy workload's exclusive shell share could sit near the knee. Measure or bound it.
- **§5.** The WSL append probe and the thread-local attribution bug are excellent implementation reporting. The mock's shared-transcript directive-collapse limitation is clearly explained and honestly propagated to experiment design.
- **§6.1.** Report the regression fit (least squares over cell medians? all 240 points?), R², and per-cell dispersion. p95 − p50 = 2 ms is stated for parallel only; give the serial/reject dispersions too.
- **§6.5.** Nice experiment. The buffer-window fallback covered "by a unit test rather than by this harness" is acceptable, but state the buffer size in events/turns so the ~1,500-turn claim is checkable.
- **§6.8.** The Jain-index discussion (scalar fairness index is misleading here) is a good methodological point; keep it.
- **§6.9.** 204 turns / 3 cycles is modest for a "lifecycle durability" claim; fine as a smoke test, phrase it as such.
- **§6.11.** The test-suite paragraph (3,534 tests, browser suite, authorization table parameterized over routes) is credible and unusual; consider promoting the route-table default-closed design to §5.
- **§7.** The four open problems are well grounded in CSCW theory and honestly framed as agenda, not claims. §7.3's "accountability or alarm fatigue" question is the best sentence in the discussion.
- **References.** [10], [11], [12] are placeholders; [11] contains untranslated Korean ("비특허문헌 1"); [26] venue unverified. Understand this is a draft, but these must be complete for review. The absence of any DB concurrency-control citation (W4) is the substantive gap.

### Minor / nits

- §5 says the reject retry wait "is measured from the server's first 409"; §6.1 says "from B's first submission attempt (for the reject arm, from the first 409)". Pick one phrasing; as written, whether the first (rejected) POST's latency counts is ambiguous.
- "Coagora, serial mode (the system's pre-existing shipped mode; still the default)" appears with slight variations ~5 times; once in §3 and once in §6 Setup suffices.
- The `[[bench ttft= tok= n= work= fwrite= …]]` directive syntax appears in §5 before the harness is introduced; forward-reference Appendix A.
- Slope reported as 1.028/1.027 in the table but 1.03/1.03 in abstract and 0.00/-0.002 elsewhere — fine, but state rounding policy once.
- "L = 30 s cell" was collected in a second session with a 56 ms constant shift; the L=30 parallel cell (0.235 s) therefore isn't comparable in absolute terms to the other three parallel cells — the text says this, but the table should footnote the session boundary.
- Consider renaming "reject-and-retry"'s tax as "phase penalty" consistently; "tax" appears with three different value lists (abstract: 79–232 ms; §6.1: +232/+182/+79/+190 ms).

### Assessment of artifact

Based on the described artifact (not executed by this reviewer): committed raw data per figure, standard-library-only harness, no-credential offline execution for 9 of 11 scripts, isolated workspaces. If this holds up, it clears the bar for an artifact badge easily. The [NOTE] about anonymization for double-blind must be resolved before submission — the current draft names the repository layout throughout.

### Overall

A real problem, an honest instrument, and rare internal validity — undermined by headline framing around a result the architecture makes inevitable, an unresolved tension in what the serial baseline can do (W2/Q1), an unmitigated semantic-confusion channel that is simultaneously a correctness and security issue (W3/W5), and missing positioning against five decades of concurrency-control literature (W4). All four are addressable in one major-revision cycle, and I would like to see this paper again.
