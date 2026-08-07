# Reviewer Simulation Round 2 — 09-full-paper-draft.md (v0.9)

> 2026-08-07. 대상: `09-full-paper-draft.md` v0.9 (Phase 0–3 리뷰 대응 완료본).
> **이번 라운드의 렌즈는 CHI Papers다.** 1차 시뮬레이션(`13-reviewer-simulation-v0.8.md`)은 CSCW/UIST 시스템 트랙 내지 ICSE/FSE 관점이었고, 그 관점의 지적(W1–W6)은 대부분 닫혔다. CHI 렌즈로 바꾸면 결정 축이 이동한다: 기술적 건전성 → **인간 증거의 부재와 기고 유형(contribution type)의 정합성**.

## 한국어 요약 (TL;DR)

- **총평: 기술 논문으로서는 1차 리뷰의 요구를 거의 전부 수치로 닫았다.** W2(serial/injection 정합성)는 boundary-density 실험으로 정면 해소, W3(의미적 혼선)은 완화책 구현 + 라이브 0/40으로 오히려 헤드라인 결과가 됐고, W4(DB 문헌)·W5(위협 모델)·W6(민감도)도 반영됐다. 자기 수치를 스스로 정정한 것(38% 철회, 1.49× 이력 불변, n=236/240 공개)은 리뷰어 신뢰를 크게 올린다.
- **그러나 CHI 심사로는 경계선이다.** 10개 RQ 전부가 기계로 답해지는 질문이고, 사람은 §7의 어젠다에만 등장한다. "teams need…"라는 동기와 CSCW 이론 프레이밍에 비해, 사용자-지각 결과(응답성 체감, 혼선의 실제 피해, 개입 행동)는 하나도 측정되지 않았다. CHI에서 이 논문의 가장 CHI다운 내용(§7.1–7.4)은 전부 future work다.
- **권고: (a) 소규모 first-use study 1개를 추가해 CHI를 정조준하거나, (b) UIST/시스템 트랙으로 타겟을 옮기고 현 평가를 유지하거나. 둘 중 하나를 결정해야 한다.** 어느 쪽이든 저비용으로 닫을 수 있는 잔여 항목(스테일니스 미측정 = 1차 Q4 미해결, 혼합 워크로드 공정성 = Q5 잔여, 현실적 혼선 기저율, 통계 검정 부재)과 본문 결함 4건(§6.10 헤더 반복 횟수 불일치, §6.11 자기모순 문단, 결론의 "write-heavy" 오기, abstract 40/40 무단서)은 이번 사이클에서 고친다.
- **점수(CHI 기준, 지시적):** Originality 4.5 · Significance 4 · Research Rigor: 시스템 평가 4.5 / 인간 증거 1 · Recommendation **3.0 (Neutral, 경계선)** — first-use study가 붙으면 4.0 이상으로 움직일 논문. UIST라면 지금 상태로 champion 후보.

---

## 전 라운드 지적 처리 현황 (리뷰어 확인)

| 항목 | 1차 지적 | v0.9 상태 |
|---|---|---|
| W1 | mock 헤드라인이 true-by-construction | **대부분 해소.** §6.10이 12회 반복·비겹침 범위·제공자 동시성 곡선(2.68×)으로 승격, mock은 "검증"으로 강등. 잔여: abstract 첫 수치가 여전히 mock slope이고 mock 표기가 없음 (아래 R2-W2) |
| W2 | §6.1 vs §6.10 injection 모순 | **완전 해소.** injection 활성 상태 공개 + boundary-density 실험(기울기 1.041, R²=0.99995, k=1 셀 0.5% 재현)이 모범적. 이번 라운드 최고의 개선 |
| W3 | 의미적 혼선 무대응 | **해소, 그 이상.** `--turn-scoping` 구현 + 음성 대조군 목 절제 + 라이브 26/40→0/40. 38% 단발값 철회도 옳은 판단. 잔여: 현실적(비최악) 워크로드의 기저율 미지 (R2-W3) |
| W4 | DB 동시성 제어 문헌 부재 | **해소.** §2.4 위치 지정 문단, [32]–[35] 인용, write-skew를 §4.3·Limitation 3까지 관통시킴 |
| W5 | 위협 모델 부재 | **해소.** §7.5 신설, 보장/비보장 표, AML.CS0035 대칭 적용 |
| W6 | 스케일·민감도 | **대부분 해소.** retry 간격 2점, N∈{2,4,8}, 이력 길이 축(1.49→1.47 반증), 셸 operating point. 잔여: [TODO: S1] 합성 워크로드 |
| Q1–Q3, Q6–Q8 | — | 해소 (Q3는 endpoint 사양 직접 측정으로 초과 달성) |
| **Q4** | 스테일니스 측정 | **미해결.** 여전히 공개만 하고 측정하지 않음 (R2-W4) |
| **Q5** | 혼합 워크로드에서 effect-layer FIFO × admission 공정성 상호작용 | **부분.** 셸 lock wait 4.1 s가 측정되어 상호작용이 실재함이 오히려 드러남; 정면 실험은 없음 (R2-W5) |

---

## Review (as submitted to the PC)

**Paper:** Coagora: Multiplayer Coding Agents via Concurrency Contracts for Synchronous Multi-User Sharing of a Single LLM Agent Session

**Venue:** CHI Papers. **Reviewer expertise:** 3/4 (CSCW, interactive systems; working knowledge of LLM agents; not a database-systems specialist).

**Recommendation:** 3.0 — Neutral / borderline. I want this work in the field. Whether I want it *at CHI in this form* depends on one axis, stated below, that the authors can move in one revision cycle.

### Contribution and benefit (my reading)

An artifact contribution (a working multi-user shared agent session under a novel concurrency contract) plus an empirical systems contribution (a within-system three-contract comparison with committed raw data), framed by a CSCW-theoretical agenda. The benefit claimed is to teams: intervene while the agent works, and a defined rule when two people speak at once.

### Summary of the submission

(As in round 1, updated.) The paper proposes "parallel inference, serialized side effects" for multiple humans sharing one live LLM coding-agent session, positions it in a four-axis design space against nine systems, and implements it as a switch inside Coagora, whose shipped default is the serial contract. New since the prior version: the serial baseline's mid-run injection behaviour is fully characterized (boundary-density law, slope 1.041, R² ≈ 1); the reject penalty is shown interval-bounded at two retry settings; parallel flatness survives 8 users at +39%; the semantic-confusion rate is *withdrawn* as a point estimate and replaced by a distribution, then addressed with a per-turn prompt-scoping mitigation that eliminates cross-user file writes in 40/40 live turns (against 26/40 without); the token premium is shown history-invariant (1.49×→1.47×) with an arithmetic argument; a live endpoint-concurrency curve (2.68× at N=8) bounds what any contract can deliver; and a trust-model section tabulates what the contract does not defend.

### Strengths

- **S1. The revision behaviour itself is exemplary.** The authors re-ran their own headline numbers, found three of them wrong or unstable (the 38% worst case, the "premium grows with history" concession, n=240), and corrected the paper against their own interest in each case. The e2b consistency check (k=1 reproduces the grid cell to 0.5%) is the kind of internal replication almost no submission does.
- **S2. The strongest objection from round 1 became the strongest section.** §6.1 no longer measures serial at a configuration the reader can't interpret; it names the axis (boundary density), measures it, and derives when serial approaches parallel and when it approaches L. This is now a finding, not a comparison.
- **S3. The mitigation result is the paper's most consequential number for practice.** 26/40 → 0/40 cross-user file writes, with own-task completion *rising* (34→40) and turn span falling, under verified overlap (≥65% coverage) and a passed negative control in the mock ablation. The "both tasks done" failure-mode discovery (turns doing their own *and* the neighbour's work) is a genuinely new observation about shared-transcript agents.
- **S4. Internal validity and reproducibility remain the best I have seen in this literature** (same binary, switch-only arms; committed raw JSONL; drift control; per-script artifact table).
- **S5. §7.5's trust table and the §2.4 classical-CC positioning** convert two round-1 liabilities into clear, bounded claims.

### Major weaknesses (round 2)

- **R2-W1. The humans in this multiplayer system never appear, and at CHI that is the decision axis.** Every dependent variable in §6 is machine-side: TTFT, slopes, token counts, lock waits, bijections. The motivation (§1), the design space's D4 axis (intervention), and the entire discussion (§7.1–7.4) are about human behaviour — grounding, arbitration, accountability — none of which is observed even once. The paper says plainly that it "establishes that the contract is implementable and what it costs, not that teams using it collaborate better" (§6.11). I respect the honesty, but that sentence concedes the CHI question. Two remedies, either acceptable: (i) a small first-use study — even 3–5 teams of 2–3 doing one realistic task under both contracts, with incident coding and post-session interviews — would ground the latency and confusion numbers in experienced difference and let §7's hypotheses make first contact with data; (ii) reframe explicitly as an artifact/systems contribution and defend evaluation-by-demonstration (Ledo et al., CHI 2018), accepting that some ACs will still balk. As submitted, the paper is a systems paper wearing a CSCW introduction, and I can construct the rejection it will receive from a less sympathetic panel: "no user evaluation of an interactive multi-user system."
- **R2-W2. The abstract's strongest sentences outrun their evidence class.** "A second user's time-to-first-token becomes independent…" leads the abstract with a mock-derived slope, unmarked as validation, and "naming each turn's own request in its prompt removed that in 40 of 40 live turns" carries no scope note although §6.7 itself is careful ("one model, one workload, deliberately confusable pair"). The body earned its hedges; the abstract should inherit them. Cheap fix, but reviewers read the abstract first and calibrate trust there.
- **R2-W3. Every confusion rate in the paper is measured at or near the worst case; the realistic base rate is unknown.** The adversarial mock *always* answers the newest question; the live scoping arms use instructions that "differ only in a tag and a filename." That was the right design for demonstrating the mitigation under maximal pressure, but the paper cannot currently answer the question a practitioner will ask first: *how often does this happen when two users do normally distinct work?* One `off` arm with realistically distinct tasks (n≈20) would bound the field rate and either shrink the problem (strengthening the deployment story) or show it persists (strengthening the mitigation's importance). Either result helps.
- **R2-W4. Round-1 Q4 remains unanswered: snapshot staleness is disclosed, never measured.** With live turn spans of 198–413 s (§6.4), the staleness window is not hypothetical — a turn can run for minutes against a snapshot missing another turn's committed effects. The instrumentation to count it appears to exist already (context generations, §6.6; commit ordering, §4.3). Report, at minimum: in the live runs, how often a turn committed after another turn's commit that its snapshot lacked, and one qualitative example of whether any answer visibly rested on stale state. "We disclose rather than solve" was acceptable once; after a revision that measured everything else, the one unmeasured disclosure stands out.
- **R2-W5. The shell measurement makes the round-1 Q5 interaction real, then leaves it hanging.** §6.4 now shows shell lock waits of 4.1 s at a 0.094 effect share, and §4.4's strict-FIFO-no-overtaking means a queued exclusive shell blocks *subsequent file effects of other users*. So a shell-heavy user demonstrably can add seconds to a file-heavy user's effect latency — the exact cross-user unfairness §6.8's admission gate does not govern (it gates dispatch, not the effect queue). One mixed-workload cell (A runs 5 s shells, B writes files; report B's effect-wait distribution against a B-alone baseline) would price the "deliberate sacrifice" §4.4 announces. Without it, the fairness story is complete at the admission layer and silent at the effect layer.
- **R2-W6. No inferential statistics anywhere.** Medians, ranges, and R² are reported carefully, but key contrasts that will be quoted (26/40 vs 0/40; 14/20 vs 20/20 both-correct; 76 ms vs 1,807 ms) carry no test or interval. With these effect sizes the tests are a formality (Fisher's exact on 26/40 vs 0/40 is p < 10⁻⁸), which is precisely why omitting them is an unforced error. One sentence of policy in Setup plus exact tests / bootstrap CIs at the quoted contrasts suffices; nothing needs re-running.

### Internal inconsistencies to fix (found in this pass)

1. **§6.10 header contradicts its own body.** The parenthetical still reads "5 repetitions for the ranking check, 3 for token accounting" while the text reports "twelve repetitions per contract" and "six repetitions per contract." A leftover from the pre-revision draft; as written it looks like the n was inflated between header and body.
2. **§6.11 contradicts itself about the flake.** The paragraph first states "no failures in the run reported here (the property-based code-index flake … did not trigger)," then ends "The single failure is a property-based test over the code-index component…". Delete the stale second passage.
3. **Conclusion mislabels the collapse regime.** "in write-heavy regimes (down to 1.10× effective parallelism)" — §6.3's 1.10 is at ~90% *exclusive shell* share; §6.3 itself shows write-heavy workloads never leave ~1.99. Say "effect-heavy (exclusive-share ≥ 0.9)".
4. **Conclusion drops the ablation marker the abstract gained.** "no file corrupted by a race (8.2% unlocked → 0%)" reads as a field rate again; the abstract's "forced-overlap ablation" phrasing should be echoed.

### Questions for the authors

1. **(R2-W1)** Which contribution type are you claiming at CHI? If artifact/systems, state it and defend the evaluation methodology explicitly; if empirical-HCI, where is the human data?
2. **(R2-W3)** What is the cross-user interference rate when the two users' tasks are realistically distinct (different modules, different verbs)? Even n=20, off-arm only.
3. **(R2-W4)** From the existing live-run logs: how many turn commits were preceded by another turn's commit absent from the committing turn's snapshot? Did any produce a visibly stale answer?
4. **(R2-W5)** Under strict FIFO with no overtaking, what does one user's 5 s shell do to another user's file-effect latency? Is the answer "up to one shell duration," and is that acceptable at cap = 4?
5. **(minor)** §6.7's live arm judges confusion by file side effects. Text-level misdirected *answers* under a live model rest on the old 3-of-3 spot check; do the 40 scoping-arm transcripts allow a text-level count too, for free?
6. **(minor)** The `honor`-arm argument says scope reached "each of 500 concurrent turns" — is that 5 runs × 100 turns? State the arithmetic; as written the number appears from nowhere.

### Minor / nits

- The v0.9 changelog block at the top (≈1,200 words) is author-note material; move to a separate changelog file before anyone else reads this. It currently *is* the first impression.
- [TODO: S1] (field-log workload mix) and the anonymization [NOTE]s remain; Table 1 still says "[NOTE: render as a proper table/figure]".
- References: [1] and [21], [22] lack authors; [3] is "Baumann et al." with no first names; [25]'s venue caveat is handled well — do the same normalization pass for the rest.
- Abstract: "What sharing *creates*, asymmetric grounding and multi-party arbitration, is our agenda" — good sentence; consider making it the *second*-to-last, ending instead on the mitigation result, which is the paper's most memorable number.
- §6.1's "the ratios … merely record where our measurements stop" is exactly the right framing; consider promoting that sentence style to the abstract's slope claim.
- "Coagora" appears in the title and Table 1 rows unanonymized — flagged already, repeating because it fails double-blind as-is.

### Scores (indicative, CHI form)

- Originality: 4.5 — the coordinate is genuinely empty and the boundary-density and both-tasks findings are new.
- Significance: 4 — conditional on the field agreeing that multi-user sessions matter; the motivation section argues this well.
- Research rigor: split verdict — systems evaluation 4.5 (best-in-class internal validity, self-correcting); human evidence 1 (absent by the paper's own admission).
- Presentation: 4 — dense but disciplined; fix the four internal inconsistencies and the changelog block.
- Reproducibility: 5.

### Overall

Round 1 asked whether the numbers could be trusted; this version answers with re-measurement, self-correction, and two results (boundary-density law, scoping mitigation) stronger than anything in the original submission. The remaining gap is not in the paper's execution but in its address: as a CHI paper it is missing its humans, and as a systems paper it is missing nothing important. The authors should either add the smallest study that lets §7 touch data, or send this excellent systems paper to a systems venue. I would be pleased to see it again either way, and at UIST I would champion it now.
