# CHI Reviewer Assessment of the Current Paper Draft

**Review date:** 2026-08-13  
**Primary manuscript reviewed:** `09-full-paper-draft.md`  
**Submission derivative checked:** `21-chi-submission-draft.md`  
**Study materials checked:** `18-first-use-study-protocol.md`, `22-study-run-kit.md`  
**Review stance:** CHI paper reviewer, evaluating the manuscript as it could be submitted today

## 한국어 요약 (TL;DR)

- **지금 제출한다면 판정은 Reject / 대폭 수정 후 재제출이다.** 프로젝트 자체가 약해서가 아니라, 좋은 시스템 연구에 비해 CHI가 요구하는 인간 증거가 아직 없고 한 논문이 너무 많은 일을 하려 하기 때문이다.
- **가장 먼저 해결할 문제는 분량이다.** 서론부터 결론까지 표를 포함하면 약 21,000단어, 표와 헤더를 빼도 약 19,300단어다. CHI 논문으로는 문장 다듬기 수준이 아니라 논문의 절반가량을 부록·보충자료로 옮기는 구조 개편이 필요하다.
- **§6.11의 스터디 더미 섹션은 좋은 실행 설계지만 아직 증거가 아니다.** 스터디 결과가 나오면 표만 채우지 말고 초록, 기여, 논의, 한계, 결론까지 그 결과에 맞춰 다시 써야 한다.
- **핵심 HCI 기여를 하나로 좁혀야 한다.** 추천하는 중심 주장은 “하나의 상태 있는 코딩 에이전트를 여러 개발자가 공유할 때, 대기시간 감소와 공유 컨텍스트의 의미적 모호성, 그리고 사용자의 이해·통제 사이에 어떤 절충이 생기는가”이다.
- **가장 강한 발견은 속도보다 의미적 오염이다.** 물리적으로 트랜스크립트와 파일 쓰기가 안전해도 모델은 다른 사람의 요청을 수행할 수 있다. 이 물리적 무결성과 의미적 정확성의 차이가 논문의 중심에 와야 한다.
- **현재 11개 RQ는 4개 정도로 합치는 편이 낫다.** 응답성, 무결성·비용 경계, 의미적 오염·완화, 실제 사용자의 인식·행동으로 재구성할 수 있다.
- **first-use study에는 보완할 설계 위험이 있다.** 공동 인터뷰가 개인 의견을 덮을 수 있고, 진행자가 사후에 사건을 고르면 선택 편향이 생기며, freeze probe 자체가 상대방을 의식하도록 학습시킬 수 있다. Module C는 항상 마지막이라 순서 효과와도 분리되지 않는다.
- **4–6쌍의 짧은 연구로 말할 수 있는 범위를 지켜야 한다.** 첫 사용에서 무엇을 알아차리고, 어떻게 귀인하며, 어떤 규칙과 보호장치를 요구했는지는 말할 수 있다. 생산성 향상, 장기 채택, 큰 팀, 늦게 합류한 사람, 조직 수준의 협업까지 일반화할 수는 없다.
- **원본과 제출용 파생본이 이미 어긋나 있다.** `09-full-paper-draft.md`에는 새 §6.11 구조가 있지만 `21-chi-submission-draft.md`에는 반영되지 않았다. 한 파일을 단일 진실 공급원으로 삼아 익명 제출본을 생성해야 한다.
- 스터디를 엄밀히 실행하고, 반대 사례까지 포함해 결과를 통합하고, 본문을 약 9,000–10,000단어로 줄이며, 상호작용·아키텍처 그림을 보강한다면 **강한 CHI 후보가 될 가능성이 충분하다.**

## 1. Overall verdict

**Recommendation if submitted in its current state: Reject / Resubmit after substantial revision.**

The technical core is unusually strong. The paper identifies a real and timely interaction problem, implements a concrete concurrency contract, exposes an important difference between physical transcript integrity and semantic task correctness, and supports its systems claims with more transparent evidence than most CHI systems papers. The negative result around cross-turn semantic contamination is particularly valuable.

However, the present manuscript is not yet a competitive CHI submission for three overriding reasons.

1. **It is far too long and unfocused for the size of its HCI contribution.** A rough Markdown count from Introduction through Conclusion is about 21,000 words including table text, or about 19,300 words after excluding table rows and headings. This is not a polishing issue; it signals that the paper is trying to carry a design-space paper, a concurrency-systems paper, an artifact validation report, a failure analysis, and a human study in one submission.
2. **The HCI evidence is still missing.** Section 6.11 is a well-prepared placeholder, but a placeholder is not a result. The current paper demonstrates that the system works and that semantic failures can occur; it does not yet show how people understand, coordinate through, trust, or reject the interaction model.
3. **The primary contribution is not yet stated sharply enough.** The manuscript currently reads as a very good systems paper surrounded by an ambitious CSCW/CHI research agenda. CHI needs one clear account of what this system teaches us about human interaction with shared coding agents.

These issues are too large for a normal revise-and-resubmit cycle. The human study must be run and analyzed, the argument must be reorganized around its findings, and roughly half the manuscript must be removed or moved to supplementary material. If those changes are made well, I would consider the work a strong candidate for a future CHI submission.

## 2. Reviewer scorecard

| Criterion | Current assessment | Reason |
|---|---:|---|
| Significance to HCI | 3/5 | The problem is important and timely, but its human consequences are not yet demonstrated. |
| Originality | 4/5 | The contribution is not a new concurrency primitive, but placing a concurrency contract inside one live agent session is a novel and useful interaction architecture. |
| Technical research quality | 4.5/5 | Strong instrumentation, explicit invariants, raw artifacts, replications, boundary tests, and candid negative findings. |
| Human-subjects research quality | 1/5 today | The protocol is promising, but no participants, observations, or analyzed results exist yet. |
| Presentation clarity | 2/5 | Individual passages are clearer than before, but the manuscript remains much too long, repetitive, defensive, and evaluation-heavy. |
| Prior-work positioning | 3/5 | Broad coverage, but the system survey is not systematic enough to support several universal claims. |
| Reproducibility | 4.5/5 | Excellent local artifact trail; the anonymous submission artifact and stable archival path still need preparation. |
| Overall | Reject in present form | Potentially strong after the study, a major reduction in scope, and an HCI-centered rewrite. |

My confidence in this assessment is high for paper structure, systems evaluation, and CHI framing, and moderate for the eventual human-study outcome because that evidence does not yet exist.

## 3. What is already strong

### 3.1 The paper addresses a genuine interaction problem

The central question—what should happen when two people act concurrently through one stateful coding-agent session—is both concrete and consequential. It is not merely a faster-chat problem. Concurrency changes ownership, awareness, interruption rights, context interpretation, and responsibility for side effects. This is legitimate HCI terrain.

### 3.2 The concurrency contract is explicit and inspectable

The paper does not hide coordination policy inside implementation details. It distinguishes reject, serialize, isolate-and-merge, and parallel-with-exclusive-effects contracts, then specifies what is concurrent and what remains protected. This gives readers a useful vocabulary for reasoning about shared agent sessions.

### 3.3 The technical evaluation is unusually transparent

The draft reports repaired implementation seams, re-runs, negative findings, mock-versus-live boundaries, raw measurements, test coverage, and limits of generalization. The willingness to revise an earlier claim about history-length cost is a strength. The underlying work appears careful and auditable.

### 3.4 The strongest finding is not the speedup but the semantic failure

The distinction between physical integrity and semantic correctness is the most important contribution in the paper. A session can preserve a valid transcript and serialize file effects while a model still follows another participant's instruction. The fact that prompt scoping works for one model but remains imperfect for another makes the result more credible and more consequential. This finding should be nearer the center of the paper.

### 3.5 The planned study is appropriately modest in several respects

The study protocol correctly avoids claiming team-performance improvement from four to six pairs. It treats questionnaire responses descriptively, uses system logs as an event-selection and checking instrument rather than as a human outcome, includes counterbalancing for the two main conditions, and plans to downgrade the work to pilot observations if fewer than four pairs finish. Those are sound choices.

## 4. Fatal issues to resolve before submission

### 4.1 P0 — Reduce the main paper by at least half

CHI's published guidance says that paper length should be commensurate with contribution, describes roughly 7,000–8,000 words as typical for long papers, and warns that papers over 12,000 words are excessively long unless the contribution genuinely demands it. The current manuscript is around 21,000 words before references. At that length, the draft is vulnerable to an assisted desk rejection before reviewers engage with its strongest ideas.

The solution is not another sentence-level edit. The manuscript needs a new information hierarchy.

Keep in the main paper:

- one motivating shared-session scenario;
- the concurrency-contract design and the physical/semantic distinction;
- one concise architecture section;
- the responsiveness result;
- the integrity/performance boundary;
- the semantic contamination and mitigation result;
- a compact bundle of the remaining correctness checks;
- the completed first-use study;
- discussion and limitations directly supported by those results.

Move to an appendix or anonymous supplement:

- full benchmark grids and sensitivity runs;
- retry, compaction, replay, fairness, lifecycle, and authorization procedures at their current level of detail;
- raw script inventories and command instructions;
- extended implementation-repair history;
- exact test-suite enumeration;
- secondary live-model re-runs not needed to understand the main result;
- review-response archaeology such as “earlier drafts said,” “after reviewers asked,” and “we re-ran the repaired seam.”

The final paper should explain what is true now. It should not read like a chronological rebuttal to earlier internal reviews.

### 4.2 P0 — Do not submit until Section 6.11 contains real evidence

The new dummy section is useful for planning, but the current manuscript still contains no observed human outcome. This matters because the introduction and discussion raise inherently human questions: shared awareness, attribution, interruption authority, grounding, trust, and coordination norms. Systems measurements cannot answer those questions.

After the study, the paper must do more than replace cells in the result tables. The findings must change the abstract, introduction, contributions, discussion, limitations, and conclusion. If the study finds that participants do not notice cross-user effects, that becomes a central safety and visibility result. If they reliably notice and repair them, the design implication is different. If teams dislike parallel interaction despite lower waiting time, that should revise the paper's framing rather than be buried as an inconvenient observation.

The manuscript should make only the claims the study can support. This is a first-use, two-person, short-session study. It cannot establish sustained adoption, productivity improvement, long-term team norms, organizational fit, or behavior in larger groups.

### 4.3 P0 — Choose one primary HCI contribution

The draft currently offers too many contribution identities at once: a design space, a concurrency contract, an implementation, a performance model, a correctness analysis, an artifact, a semantic-failure result, and a human research agenda. This weakens the central claim.

I recommend making the primary contribution:

> A shared-session interaction architecture that allows developers to act concurrently through one stateful coding agent, together with evidence showing the tradeoff between lower waiting time, shared-context ambiguity, and users' ability to understand and manage that ambiguity.

The concurrency mechanisms, evaluation harness, and system survey should support that claim. They should not compete with it as separate headline contributions.

## 5. Major research concerns

### 5.1 The design-space survey does not justify universal claims

Statements such as “Every deployed AI coding agent assumes a single user” and “we know of no published study” are too broad without a reproducible search procedure. The nine-system comparison is useful, but it is not yet a systematic review. Vendor documentation also changes over time and can be ambiguous about session identity, concurrency, and context sharing.

The paper should:

- replace universal language with a scoped claim such as “among the systems in our survey”;
- report inclusion and exclusion criteria, search date, search terms, and how ambiguous cases were handled;
- archive the exact documentation pages or provide stable snapshots where permitted;
- distinguish product documentation, issue requests, prototypes, and peer-reviewed research;
- avoid using an apparently empty cell in a small sample as proof that the design point has never been explored.

### 5.2 The novelty is the interaction-level placement, not the locking mechanism

Database transactions, critical sections, sequence numbers, and replay are established techniques. The paper now acknowledges this, which is good. The CHI novelty must therefore be stated at the interaction level: what becomes possible when speculative inference is concurrent, committed state is ordered, and visible effects are attributed inside a shared human-agent session?

Without completed human evidence, this can look like known concurrency control applied to a new application. With human evidence, it can become a contribution about how technical ordering choices shape awareness, control, and responsibility.

### 5.3 Eleven research questions fragment the argument

Many current RQs are implementation validation questions rather than independent research questions. Replay, compaction, lifecycle, authorization, and fairness matter, but presenting each as a separate RQ makes the paper feel like a test report.

A stronger structure would use four RQs:

1. **Responsiveness:** How do shared-session concurrency contracts change waiting and overlap?
2. **Integrity and cost:** Which operations must remain exclusive, what correctness properties result, and where does the performance benefit disappear?
3. **Semantic interaction:** When concurrent turns share context, when do requests contaminate one another, and how well does turn scoping mitigate that behavior?
4. **First use:** What do pairs notice, infer, and do when using serial and parallel shared sessions?

The smaller technical checks can be grouped as validation evidence under RQ2 rather than promoted to separate research questions.

### 5.4 The main comparison omits a consequential alternative

The design space presents isolate-and-merge as an alternative, but the central evaluation compares reject, serial, and parallel contracts within one shared context. This is legitimate if the paper is explicitly about **contracts for one shared live context**. It is not enough to imply that parallel shared context is generally preferable to per-user branches.

Either add a carefully bounded isolate-and-merge comparison, including its context and merge costs, or narrow the claim throughout the paper. Given the current length, narrowing the claim is preferable.

### 5.5 The deterministic slope result is verification, not the main discovery

The 0-versus-1 latency slope under a scripted mock is valuable evidence that the implementation follows its contract. It is also close to true by construction. Statistical tests on repeated deterministic schedules do not turn this into a population-level empirical discovery.

Use the mock grid as contract verification. Lead the CHI story with the live workload, the interaction consequences, and the semantic failure. Clearly separate descriptive runtime variation from inferential generalization, and do not overemphasize p-values for mechanically generated repetitions.

### 5.6 External validity remains narrow

The live evaluation uses a small number of models, one local endpoint environment, synthetic tasks, and mostly two concurrent users. Hardware scheduling and provider batching can dominate latency. The human study also uses pairs and short, separable tasks. Therefore, the paper should avoid general claims about “teams,” production-scale collaboration, or multi-user behavior beyond pairs.

The mock experiments with four or eight users demonstrate mechanism scaling, not ecological scaling. State that distinction plainly.

### 5.7 Physical integrity must never be mistaken for task correctness

The system prevents torn writes and orders committed history, but it does not guarantee that a turn follows its owner's request. On the second tested model, prompt scoping still leaves cross-task behavior. That means the shipped mitigation is a heuristic, not a safety boundary.

This distinction should be visible in the title framing, abstract, contributions, system description, results, and conclusion. For deployment, the paper should discuss stronger effect-level defenses: per-turn previews, explicit confirmation for cross-owned files, turn-specific capability scopes, or sandboxed effects. These can remain future work, but the present system should not be described as semantically safe.

### 5.8 The trust model is narrower than the deployment rhetoric

The manuscript assumes mutually trusting participants, coarse read/write authority, and directory-scoped effects. Background processes and indirect effects can escape the simple lock model. Even cooperative collaborators can unintentionally cause semantic contamination.

This is acceptable for a research prototype, but the conclusion should not imply deployment readiness. Separate protection against accidental concurrency from protection against malicious or compromised participants.

## 6. Review of the planned first-use study

The protocol is a credible exploratory first-use study, but it does not validate every human claim raised in the discussion. Its strongest attainable contribution is a careful account of what pairs can perceive, how they attribute system behavior, what coordination routines they invent, and which safeguards they request.

### 6.1 Sample size and claim boundary

Four to six pairs can support an exploratory qualitative account and descriptive probe counts. It cannot support a general contract preference, a reliable population difference, or claims that parallel interaction improves collaboration. Preserve the planned no-significance-test stance and report distributions and cases rather than only aggregate percentages.

If fewer than four pairs complete the protocol, label the evidence as pilot observations exactly as planned.

### 6.2 Joint interviews may suppress individual disagreement

The event-based interview is conducted jointly. One participant may supply an explanation before the other answers, overwrite the other's memory, or discourage criticism of a partner. This is especially serious for attribution, interrupt authority, and social norms.

Before joint discussion, collect a brief private written response from each participant for the selected events, or ask each person to answer independently before allowing discussion. Preserve disagreements as data instead of resolving them into a single pair account.

### 6.3 Facilitator-selected events introduce selection bias

The facilitator chooses three to five logged events during the break. Even with a priority guide, this creates discretion over which successes, failures, and participants are discussed.

Predeclare a deterministic selection rule—for example, the first eligible event of each priority class plus the highest-overlap event—and report the eligible event universe, the selected subset, and any deviation. Pilot whether the four-minute break is enough to apply the rule reliably.

### 6.4 Freeze probes are reactive

Repeatedly asking participants what their partner is doing may teach them that partner awareness is important and change how they monitor the shared session. The observed awareness may therefore be an upper bound produced partly by the measurement itself.

Report this as probe reactivity. Keep the probes sparse, use fixed timing or predeclared triggers where possible, and compare them with unsolicited awareness statements from the interaction and delayed interview recall.

### 6.5 Ground truth for “what the partner is doing” needs an operational definition

Logs can show a submitted request, tool call, effect, or completion, but the participant may currently be reading, planning, waiting, or talking. The protocol must define what counts as the partner's “actual current action” at the probe timestamp.

The paper should report:

- the unit being scored;
- the ground-truth hierarchy used by coders;
- whether coders were blind to condition and participant answer;
- initial agreement before consensus, not only final consensus;
- how ambiguous timestamps were handled.

### 6.6 Module C is intentionally confounded by order

The unscoped parallel condition is always last. It is therefore confounded with learning, fatigue, and increased familiarity with the interface. This may be justified to avoid contaminating the main comparison, but Module C cannot be compared numerically with the main serial and scoped-parallel conditions as if order were controlled.

Use Module C only as a targeted visibility probe: when a mechanically confirmed cross-user effect occurs, did the affected participant notice and correctly attribute it? Do not treat its contamination count as an occurrence-rate estimate or compare its detection rate directly with A/B without a strong caveat.

### 6.7 The tasks may favor the parallel contract

Short, separable TaskBook tasks are operationally convenient and help produce overlap, but they may make parallel execution look more appropriate than real collaborative programming with dependencies, shared design decisions, and evolving requirements. They also position participants mainly as prompt authors and observers rather than as developers engaged in sustained joint work.

Frame the study as first use under controlled, parallelizable tasks. Record pair familiarity and prior agent experience, and avoid generalizing the resulting norms to long-lived software projects.

### 6.8 The qualitative analysis plan is under-specified

M1 and M3 have coding categories, but M4 and M5 need a clearer analytic procedure. Before collection, specify the unit of analysis, codebook development process, treatment of emergent codes, negative-case analysis, coder roles, reflexivity, and audit trail. Do not claim saturation from four to six pairs.

Report variation across pairs. A finding that occurred in three pairs and was contradicted in two is more informative than a flattened “participants tended to” statement.

### 6.9 Ethics, language, and quotation handling must be explicit

The ethics or IRB determination must be complete before data collection and reported in the paper. If the study is conducted in Korean and the paper reports English quotations, state the study language, who translated the material, and how meaning was checked. Quotation approval is appropriate, but report it transparently because approval can sanitize criticism after the fact.

### 6.10 The study does not test several claims currently raised in discussion

The protocol does not include late joiners, groups larger than two, long-term adoption, organization-level governance, or a manipulation of interruption policy. In particular, M1 partner awareness is not evidence about asymmetric grounding for a late joiner. Remove or retain as future work any discussion claim the study cannot answer.

## 7. Presentation and paper-structure problems

### 7.1 The manuscript needs visual explanation

For a CHI audience, the paper relies too heavily on prose and tables. Add at least:

1. a motivating timeline comparing serial, isolate-and-merge, and parallel shared-session interaction;
2. an architecture diagram showing concurrent inference, shared context snapshots, ordered commit, and exclusive effects;
3. a screenshot or wireframe of attributed concurrent streams, ownership, and interrupt controls;
4. one compact result figure connecting responsiveness, effect share, and semantic contamination.

All figures should use accessible colors, readable labels, captions that stand alone, and alt text in the final submission workflow.

### 7.2 The tone sometimes reads like a rebuttal

Phrases about what earlier drafts claimed, what reviewers asked, what was repaired, and what “honesty requires” demonstrate good internal research practice, but they interrupt the final scientific story. Put provenance in an artifact changelog or response letter. State the current result, its method, and its limitation directly.

### 7.3 The title is long and systems-heavy

The current title buries the interaction contribution. Possible alternatives are:

- **Coagora: Sharing a Live Coding-Agent Session Across Multiple Developers**
- **Multiplayer Coding Agents: Sharing One Live Session Without Serializing Inference**
- **One Agent, Multiple Developers: Concurrency and Coordination in a Shared Coding Session**

The final choice should follow the human result. If visibility and attribution become the strongest finding, the title should foreground that rather than speed.

### 7.4 “One agent” needs a precise definition

The implementation uses one shared session/runtime but concurrent turn loops. Some readers will interpret each loop as a separate agent instance. Define agent identity in terms of shared session state, tools, and runtime ownership, and acknowledge that inference loops execute concurrently. Avoid relying on “one agent” as a rhetorical novelty claim unless the term is operationalized.

## 8. Versioning and submission-readiness issues

The working master and the CHI submission derivative have drifted. The master now contains the Section 6.11 study scaffold, while `21-chi-submission-draft.md` still carries the earlier structure and status language. The derivative is also slightly longer by rough count. This creates a serious risk that the study is integrated into one file but not the actual submission file.

Before further editing:

- establish one source of truth and generate the anonymized submission form from it;
- add a check that the section hierarchy and result numbers match across variants;
- remove every `[TODO]` and `[NOTE]` before submission;
- prepare an anonymous artifact snapshot without searchable repository history or identifying metadata;
- replace local paths with stable anonymous artifact references;
- verify that implementation details, line counts, and unique repository structure do not inadvertently deanonymize the authors.

The paper must remain understandable without the artifact. Scripts and raw data substantiate claims, but they cannot substitute for method and result descriptions in the paper.

## 9. Recommended new paper structure and word budget

The following structure would preserve the intended contribution while producing a readable CHI paper of roughly 9,000–10,000 words.

| Section | Target words | Purpose |
|---|---:|---|
| Abstract | 200–250 | Problem, architecture, principal technical result, principal human result, boundary |
| 1. Introduction | 850–1,000 | One scenario, research gap, four RQs, three contributions |
| 2. Related Work and Scoped Design Space | 1,200–1,400 | Position the surveyed systems and HCI novelty without universal claims |
| 3. Shared-Session Contract | 1,300–1,500 | Interaction model, invariants, physical versus semantic correctness |
| 4. System | 800–1,000 | Only architecture needed to interpret results |
| 5. Technical Evaluation | 2,200–2,600 | Responsiveness, integrity/cost boundary, semantic contamination, compact validation bundle |
| 6. First-Use Study | 1,600–2,000 | Method, findings, cases, negative cases, limits |
| 7. Discussion and Limitations | 850–1,100 | Design implications grounded in both evidence streams |
| 8. Conclusion | 180–250 | One bounded answer, no new claims |

The appendix and supplement can retain the rich engineering evidence. The main paper should optimize for comprehension, not for preserving every experiment in narrative form.

## 10. Concrete revision sequence

### Must be completed before submission

1. Run the first-use study with ethics clearance and preserve the protocol's claim boundaries.
2. Strengthen event selection, independent participant response, probe-ground-truth, and qualitative-analysis procedures before the first main session.
3. Analyze the study before finalizing the argument; let contrary results change the paper.
4. Collapse eleven RQs into approximately four.
5. Cut the paper below 12,000 words at an absolute maximum; target 9,000–10,000.
6. Reframe the primary contribution around human interaction with a shared stateful agent.
7. Scope the design-space claims and document the survey method.
8. Distinguish physical integrity, semantic correctness, and deployment security consistently.
9. Add interaction and architecture figures.
10. Synchronize the master, anonymized manuscript, appendix, and artifact.

### Strongly recommended

- Collect private per-participant event interpretations before joint interviews.
- Use a deterministic event-sampling rule and report event coverage.
- Replace p-value-heavy deterministic validation with effect sizes, distributions, and explicit verification language.
- Report first-use cases and counterexamples at the pair level.
- Reduce speculative discussion sections that the study does not address.
- Shorten the title after the primary empirical finding is known.

## 11. Questions I would ask the authors in review

1. What is the single HCI claim that would remain if the benchmark and artifact details moved to the supplement?
2. Why is a shared-context parallel contract preferable to isolate-and-merge for any task beyond the evaluated scenarios?
3. What did participants fail to notice, and what harm could follow from that invisibility?
4. How was “the partner's current action” established at each freeze probe?
5. How often did paired participants disagree in private before joint discussion?
6. How were interview events selected, and what proportion of eligible events were never discussed?
7. Which design implication is supported by observed behavior rather than derived only from the architecture?
8. What exact guarantee does turn scoping provide, given the residual failures on the second model?
9. Which current discussion claims are not tested by the first-use protocol?
10. Why does the main paper need each experiment that remains after the central responsiveness, integrity, semantic, and human results are presented?

## 12. Bottom line

This is not a weak project. It is a strong project whose current manuscript asks one paper to do too much. The systems contribution is mature enough to support a compelling CHI paper, but the HCI contribution must be demonstrated rather than promised, and the manuscript must trust its best findings enough to discard much of the surrounding validation narrative.

The most promising final story is not simply that parallel inference is faster. It is that **sharing one stateful agent changes the meaning and ownership of action: technical serialization can preserve the session while people and models still misunderstand whose request is being served.** A completed first-use study can show whether participants see that boundary and what controls they need. That would be a focused, original, and recognizably CHI contribution.

## 13. CHI criteria used for this assessment

This assessment uses the latest complete public CHI reviewing guidance available during the review, especially its criteria of HCI significance, originality, research quality, presentation clarity, prior-work engagement, contribution-proportional length, and human-subjects transparency:

- [CHI 2026 Guide to Reviewing Papers](https://chi2026.acm.org/guide-to-reviewing-papers/)
- [CHI 2026 Guide to a Successful Submission](https://chi2026.acm.org/guide-to-a-successful-submission/)
- [CHI 2026 Contributions to CHI](https://chi2026.acm.org/contributions-to-chi/)
- [CHI 2026 Papers](https://chi2026.acm.org/authors/papers/)
- [CHI 2027 Papers](https://chi2027.acm.org/authors/papers/)

The exact administrative rules should be checked again against the target year's final call before submission. The substantive diagnosis above does not depend on a minor year-to-year template change.
