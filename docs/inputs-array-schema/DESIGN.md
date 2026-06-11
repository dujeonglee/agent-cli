# Multi-Op Wire Format — Design

**Status:** design exploration, validated by single-turn bakeoffs against the
live omlx server (Qwen3.6-27B-MLX-8bit, Qwen3.6-35B-A3B-MLX-8bit), temp 0 and
0.7. **Not implemented.** Single-turn format compliance ≠ multi-turn loop
correctness; the loop surgery (§6) is unproven.

Reproducers (throwaway harnesses; register a prototype `WireFormat` at runtime,
never shipped):
- `scripts/bakeoff/proto_inputs_array.py` — pure-JSON `{thought, inputs:[…]}`
  (the first shape; superseded — see §3).
- `scripts/bakeoff/proto_md_array.py` — the **current** design (markdown
  envelope + action array). `BAKEOFF_NO_BATCH=1` selects the no-batch variant.

---

## 1. The design

A turn is two markdown sections — the prefix_md envelope the model already
emits reliably — with the action section carrying a **JSON array of flat
`{action, …params}` ops**:

```
## Thought
read auth.py and list src/

## Action
[{"action": "read_file", "path": "src/auth.py"},
 {"action": "shell", "command": "ls src/"}]
```

Completion is a **thought-only** turn — the `## Action` section omitted (the
thought is the final answer):

```
## Thought
Done — login() is implemented and tests pass.
```

Properties:

1. **Markdown envelope** (`## Thought` / `## Action`) — the validated prefix_md
   shape. Robust where a pure-JSON wrapper was not (§3).
2. **Multi-op via the array.** Independent ops in one turn = multiple elements.
   Dependent ops (a later one needs an earlier one's result) → emit only the
   first; its observation comes next turn.
3. **Explicit per-op `action`; plain param keys** (no `{tool}_` wire-key
   prefix). A single op may be a bare object (the model's natural form for one
   op); the parser treats it as a one-element array.
4. **No per-tool batch.** The op-array *is* the batch mechanism: N files = N
   `read_file` ops. `read_file_reads` / `edit_file_edits` / `code_index_queries`
   / `delegate_tasks` arrays go away (§3, §4.2).
5. **Terminal = thought-only**, parsed leniently (§4.3): an omitted, empty, or
   `None`-marker `## Action`, and a bare result-bearing object, all read as
   completion.
6. `complete` is **not** a tool; `ready_for_review` stays an op-callable tool.

---

## 2. How we got here

The shape evolved across the bakeoffs:

- **v1 — pure JSON, prefix keys, no action**: `{thought, inputs:[{read_file_path:
  …}]}`. Work compliance was excellent (§3 Exp 1), but the **terminal kept
  dropping the JSON envelope** — the model emitted a bare `{result:…}` or
  narrated its intent (§3 Exp 2/3).
- **v2 — markdown envelope + action array (current)**: keeping `## Thought` /
  `## Action` removes the envelope-drop (the markdown anchors are what the model
  reliably emits), and an explicit per-op `action` reads cleaner than prefixing
  every key inside an array. Dropping the prefix is a measured reversal of
  [wire_key_prefix_adoption] — acceptable in the array context, with one cost
  (§4.1).

---

## 3. Evidence

All temp 0.7, N=10, both models unless noted. parse_ok = the emission matches
the format (valid work array OR a clean terminal).

### Exp 1 — pure-JSON work + multi-action (proto_inputs_array)

- Format valid on 9/10 tasks = 100%; **action-leak (a stray top-level `action`
  key) = 0%** — dropping `action` did not reassert the ReAct prior.
- Multi-action solid: `multi_read_shell` 2.0, `multi_three` **3.0**,
  `mixed_indep` 2.0 — 100%, both models. Dependency restraint 100%.

### Exp 2/3 — terminal (pure JSON)

- `complete` as an array op **failed** (0–60%): envelope-drop (`{complete_result:
  …}`) and narrate (`{thought:"I should call…"}`).
- **thought-only terminal** was far cleaner (greet/finish 100% both models;
  one false-terminate at 35B). → terminal wants to be thought-only, not an op.

### Exp 4 — markdown action-array, WITH per-tool batch (proto_md_array)

- Flat multi-op = **100%** both models (multi_read_shell / multi_three /
  mixed_indep). The op-array itself is rock-solid.
- **Per-tool batch nesting broke 27B**: `two_files` / `batch_read_three` (a
  `reads` array nested inside an op) = **10%** parse_ok on 27B (a stray brace in
  the array>object>array nesting); `dependent_read_edit` (nested `edits`) = 40%.
  35B handled the nesting fine (100%).
- A prototype-prompt bug inflated earlier runs: `build_system_prompt` always
  exposes `complete` and the tool guides hardcode prefixed keys
  (`read_file_reads`). The model was following the leaked prompt, not failing.
  See §5 — the tool-guide layer is coupled to the old convention.

### Exp 5 — markdown action-array, NO batch (`BAKEOFF_NO_BATCH=1`)

Dropping per-tool batch (one target per op) **closes the 27B failure**:

| task (27B) | with batch | no batch |
|---|---|---|
| two_files | 10% | **100%** |
| batch_read_three | 10% | **100%** |
| dependent_read_edit | 40% | **100%** |

The nested-batch JSON malformation was the *only* 27B work failure, and it was
an artifact of nesting a batch array inside the op array — redundant once the
op-array exists. Removing it: all work tasks **100%**, both models.

Residual after no-batch (terminal only, not batch-related): the model still
reaches for a result-bearing completion — `## Action\nNone.` (35B) and `## Action
\n{"result":…}` (27B). Handled by lenient terminal parsing (§4.3).

### Exp 6 — Phase-2 full-loop bakeoff (real AgentLoop, mocked tools)

Real `run_loop` end-to-end (recovery, gate, multi-turn), N=3, max_turns=10,
both models, 7 tasks; baselines react 95.2% / prefix_md 90.5% completed.

- **Round 1: FAILED** (83.3% completed, 6.3 iters, 0.79 pf/run, 2.64 rec/run).
  Two bugs found and fixed: (a) the base `provider_call_kwargs` default leaked
  `json_mode=True` → markdown envelope impossible, every turn bare JSON, which
  the lenient terminal swallowed as completion (metrics looked perfect while
  no tool ran) — provider-path-only bug the proto bakeoff could not see;
  (b) the dominant honest failure: models FINISHING with the prefix_md prior
  leaking (`## Action`(empty) + `## Input\n{}`) → empty op labeled NO_ACTION →
  recovery demanded an action → 10-13 loop turns per run.
- **Round 2 (after §4.3 residue tolerance + a DONE exit in the NO_ACTION
  reminder): PASSED — 95.2% completed** (= react, > prefix_md), 4.5 iters,
  0.48 pf/run, 1.02 rec/run. The only incomplete cell (27B edit task, 33%)
  matches react (33%) and beats prefix_md (0%) on a task all formats fail in
  the mock environment (static read output makes verify-loops).
- Residual friction: pf/rec still above baselines (sporadic drift outside the
  finish phase); +1 iter is partly the structural ready_for_review gate cost.

### Exp 7 — Real-world session (DOOM build, web, 27B)

One real working session (`--response-format md_array`, 150 turns, 2 user
requests, tool mix edit 41 / shell 40 / read 36 / write 26):

- **Format failures: 1 (NO_JSON, 0.7%)** — recovered next turn; plus one
  ACTION_LOOP (B1 behavioral guard, recovered next turn). No cascades, no
  degeneration across 150 accumulated multi-turn priors. On par with the
  prefix_md real-world baseline (99 turns, 1 failure, 1.0%).
- Termination: one thought-only terminal + the ready_for_review gate fired
  exactly once. Multi-op history records and combined observations behaved.
- **Multi-op uptake is the honest gap**: 1 of 142 op-turns used a multi-op
  array (3 ops, ~2 turns saved). The format permits it; the model rarely
  reaches for it unprompted (the workload is also inherently sequential —
  write→test→fix). Verdict: stability is at parity, multi-op payoff still
  unrealized.

  **Decision (2026-06-11) — switch to default, no deprecation.** Promoted
  md_array to `DEFAULT_WIRE_FORMAT` on the parity evidence (a functional
  superset of prefix_md: every prefix_md turn is a 1-op md_array turn, plus
  multi-op is available the moment uptake improves). prefix_md is kept as a
  registered, selectable fallback — explicitly NOT deprecated this cycle, so
  there is an escape hatch if the new default regresses in wider use. The
  remaining work (raise multi-op uptake via prompting; revisit the rfr-gate
  +1-iter cost) is now default-path improvement, not a gate on adoption.

### Exp 8 — Multi-op uptake nudge (2026-06-11, prompting)

Grounding: the DOOM session (156 op-turns) had **23 runs of consecutive
read-only single-op turns** (lengths 2–5), an upper bound of ~38 turns (24%)
that could have been one multi-op turn each. Low uptake is therefore not
"correct sequential behavior" — there is real batchable opportunity the model
isn't taking. (Upper bound: the heuristic counts read_file/code_index/shell
runs; some shell steps are genuinely dependent, so the true figure is lower —
but length-4/5 read_file runs are textbook independent reads.)

Intervention (approach **B** — static steering, no loop-level nudge):
- `format_rules` gains an active decision heuristic ("before you emit, look at
  everything you intend to do; independent ops go in THIS turn") plus a worked
  3-op independent-read example (models the dominant missed pattern). The two
  guardrails are kept at equal weight so the nudge can't regress into the two
  known failure modes: dependent-batch (a later op needs an earlier result —
  all ops in a turn run before any observation) and nested arrays (broke 27B,
  §3). Rule 3 now says "N separate ops in the SAME turn".
- read_file's inline guide (the dominant single-op tool) gains a same-turn
  batch hint with the no-nesting guardrail inline; code_index already had one.
- Scope: read-only tools only. edit_file/write_file/shell are not nudged per
  tool (dependency is common there); the general heuristic covers them.

Measurement (pending live run): a Phase-2 task with genuine independent-op
opportunity (e.g. "read these 3 files, report which defines X") to measure the
uptake delta, plus the real-world multi-op rate from turns.jsonl. Regression
signal = a rise in nested-array or dependent-batch parse failures.

**Root-cause fix (live debugging of the Exp-8 ship).** On the next session the
27B emitted `{"read_file_reads": [{"path": ...}]}` — the OLD batch-wrapper key —
under md_array. It silently recovered (the `read_file_` prefix lets infer_action
restore action=read_file, and read_file accepts the batch array), so it was not
a hard failure, but it was the wrong shape. The cause was not history/prior
contamination (0 occurrences) nor a prompt leak in the inline guide (clean) — it
was the **tool's Input-JSON schema**: under multi-op, `get_tool_descriptions`
stripped the wire prefix but still advertised the batch array param
(`reads: array<object{...}>`) and batch prose ("Provide reads as a list"). The
model faithfully copied the advertised shape. Exp 8 fixed the inline guide but
missed the schema render — a guide/schema contradiction (tech debt).

Fix: `_multi_op_flat_params` unwraps the batch array param to its item fields
(generic — the item schema already exists; mirrors `wrap_single_op`), and
`_MULTI_OP_DESC_REWRITES` neutralizes the batch prose for read_file/code_index.
Now the advertised shape == the flat op the model should emit. Lesson: when a
wire format changes the op shape, the TOOL SCHEMA render is part of the prompt
surface — fixing only the inline guide leaves the authoritative (copied) shape
wrong.

**Guard line removed (live, same debugging).** A first attempt added a guard to
read_file's guide ("do NOT use a `read_file_reads` wrapper"). On the fixed
prompt the 27B STILL emitted `read_file_reads` — and the Prompt Inspector showed
the only place that token now appeared was the guard itself. The negative
mention primed the very token it forbade ("don't think of an elephant"); small
models drop the negation and latch onto the salient token. Removed it; the clean
flat schema + flat examples carry the shape without naming the anti-pattern. The
residual emission is a model prior (it occurred pre-guard too) and is benign: it
recovers via the `read_file_` prefix (infer_action → read_file, which accepts the
batch array), so the file is still read. Not chased to zero — prompt-pressuring a
model prior is whack-a-mole, and the recovery is clean. Meanwhile the B nudge
landed hard on the fixed prompt: multi-op adoption jumped from 0.6% to ~100% of
early op-turns (read_file ×6/×7/×8 batches), 0 regressions.

**Where the invented key actually came from (resolved).** `read_file_reads` is a
name we coined — the 27B can't recall it from training, and it is NOT in the
md_array prompt (0 occurrences) nor in any session's history (so not
self-reinforcement). The model CONSTRUCTS it: the prompt puts the tool name
`read_file` next to the plural noun "reads" repeatedly (the batching guidance —
"Each read_file op reads ONE file", "independent reads belong together", "full
reads burn context budget"), and the model composes `read_file` + `reads` →
`read_file_reads`. We coined the same string by the same obvious logic (tool name
+ plural param), so it converges — invention, not memorization. This explains why
the flat-schema fix didn't stop it (the seed is the WORD "reads", not the schema)
and why the guard made it worse (it added the exact token). Mitigation: reword the
md_array read_file guide to drop "reads" as a plural noun next to the tool name
("op targets ONE file", "independent files belong together", "a full file read")
— the steering is unchanged, the seed is gone. react/prefix_md keep
`read_file_reads` (their genuine wire key; single-action batching needs it).

**Empty-array terminal gap (same live session).** After `ready_for_review`
the 27B emitted "Decision: complete" + `## Action\n[]` — an explicitly empty
op array — and looped on format recovery: the lenient terminal handled `{}` and
`[{}]` but `[]` fell through to NO_JSON (valid JSON, zero dict ops). Fixed:
`[]` with a thought is a completion attempt (same family as `{}`/`[{}]`), while a
non-empty non-dict payload (`[1,2,3]`) stays a parse failure. This is the same
class as the recurring "이제 ~하겠습니다" NO_JSON finishing-transition (isolated,
1-turn recovery, no cascade) — the empty-array variant is now clean.

**Termination model reverted to `complete` (DESIGN Exp 8 conclusion).** Each of
the above finish bugs — false-terminate (the ~10% that needed the gate), the
NO_JSON finishing-transitions, the empty `[]`, and the review-instruction
mismatch that emitted a result-less `complete` and lost the deliverable — traces
to ONE origin: thought-only termination is ambiguous, and the loop kept patching
its symptoms (the ready_for_review gate, lenient-terminal special cases, an
empty-array tolerance). `complete` is the proven prefix_md/react completion verb
(99-turn validated, an explicit `result` field). So md_array reverts to it:
`exposes_complete=True`, completion is a `{"action":"complete","result":...}`
op, and a thought-only / 0-op turn is a NO_ACTION nudge (call `complete` or emit
ops) — never a silent completion. This let the whole thought-only apparatus be
removed: the loop's `_finish_terminal_turn` + `_terminal_reviewed` gate, and the
parser's lenient-terminal branches. md_array is now "prefix_md's termination +
markdown multi-op array". `ready_for_review` reverts to a model-invoked
pre-complete check (parity with the single-action formats); the review
instructions ("call complete") are correct again, so the deliverable is no
longer lost. The B multi-op nudge is orthogonal to termination and is retained.

Header-less complete (live, delegate explorer): a finishing model wrote its
reasoning then appended `[{"action":"complete","result":<full analysis>}]` with
NO `## Action` header. The header-less recovery only fired when the emission
STARTED with a bracket, so the complete op — carrying the entire deliverable in
`result` — was discarded (→ NO_ACTION → empty result, the run ended on a bare
`✓`). Fixed: extract the op array anywhere in a header-less emission, not only
at position 0 (the `any("action")` guard keeps a stray prose bracket from
becoming a spurious op). Re-validate completion reliability + multi-op adoption
on the next live run.

### Established vs not

Established (greedy + temp 0.7, single-turn): the model emits the markdown
action-array, multi-op, dependency restraint, and thought-only terminal at high
rates; **no per-tool batch is the key simplification** (removes the 27B JSON
failures). Not established: anything multi-turn / loop-level (§6), broad task
distribution, multi-turn degeneration.

---

## 4. Decisions

### 4.1 Resolved by evidence

- **Markdown envelope** (`## Thought` / `## Action`), not a pure-JSON wrapper —
  removes the terminal envelope-drop.
- **Explicit per-op `action`, flat plain params** — chosen over wire-key prefix.
  Cost: an op that drops its `action` is unrecoverable (no prefix to infer
  from) → must recover. Revisit prefix only if that proves frequent.
- **No per-tool batch** — the op-array absorbs it; removing the nesting is what
  fixed 27B. (`read_file_reads` etc. removed.)
- **Terminal = thought-only**, leniently parsed.
- `complete` not exposed; `ready_for_review` kept as an op.

### 4.2 The edit_file "exception" — also collapses

Earlier reasoning held edit_file's atomic batch as an exception. It is not:
edit positions are **hashline content-addresses** (`10#AB`), so sequential
edit_file ops re-resolve by hash and survive line-number shifts. Atomic batching
was a single-action-era optimization, not a correctness necessity, for
non-overlapping edits (overlapping edits are a model error either way). So
edit_file batch also folds into the op-array. (A single edit still carries a
`lines` array — that intra-op nesting is irreducible, and is a minor 27B JSON
risk for edit-heavy turns, separate from the per-tool *batch* nesting removed
above.)

### 4.3 Lenient terminal parsing (decided)

The model repeatedly reaches for an explicit result-bearing completion. Rather
than fight it, the parser accepts all of these as terminal:

- `## Action` omitted, empty, or a `none`/`n/a`/`null`/`nothing` marker.
- A bare object with no `action` but a `result` key → terminal, answer = the
  `result`. (A no-action op *without* `result` is a work op that dropped its
  action — that stays a work op and is measured as such.)
- Plain text with no `## Thought` header → terminal, the text is the answer.

### 4.4 Open

- **false-terminate** (~10% measured on one ambiguous task) — mitigation:
  a thought-only turn fires `ready_for_review` (re-injects task + checklist);
  only a second thought-only truly ends. Loop-level (§6).
- Final-answer location: thought text vs the `result` of a result-object
  terminal — both accepted; the loop extracts whichever is present.

---

## 5. Tool-guide coupling (an implementation cost)

The current prompt layer is coupled to the prefixed/`complete` convention:

- `complete` is injected into every prompt regardless of `active_tools`
  (a fixed completion section + `ready_for_review`'s "call before complete").
- Tool inline guides (`prompts/system_prompt.py` `_build_*_inline`) hardcode
  prefixed keys in prose (`read_file_reads`), independent of
  `render_action_input`.

So a flat / no-prefix / no-complete format is **not** just a plugin — it needs
the tool-guide rendering to be wire-format-aware (render flat `{action, plain}`
guides) and the completion section to be wire-format-controlled. The pure-JSON
v1 worked only because it reused the prefixed keys (no fight with the guides).
The bakeoff sidesteps this with a prompt post-processor (`_clean_prompt`); the
real implementation must make the guide layer format-aware.

---

## 6. Loop surgery — IMPLEMENTED (unit-tested; Phase-2 bakeoff pending)

Landed across four commits (decisions: unified dispatcher / rfr gate /
sequential + run-all + any-fail / Tool ABC hook):

- **Step 1** — `Op` + `ParsedTurn` dataclasses and the concrete
  `WireFormat.parse_turn()` default wrapper (additive, inert).
- **Step 2** — format-aware prompt layer: `multi_op` / `exposes_complete`
  flags; `get_tool_descriptions(wire_format=)` strips each tool's own prefix
  and withholds `complete`; the four inline guides render single-target
  (no-batch) variants; ask-guide no-complete variant. Single-action formats
  byte-guarded by snapshots (`tests/snapshots/tools_section_*.txt`).
- **Step 3a** — unified turn dispatch: the loop parses via `parse_turn` and
  dispatches `_dispatch_turn` → `_dispatch_op` (per-op body unchanged) →
  `_recover_unparsed`. Single-action formats = exactly one op, behaviour
  preserved (full suite green).
- **Step 3b** — N-op execution: `Tool.wrap_single_op` (flat op → canonical
  prefixed input; batch tools override, idempotent), sequential run-all,
  `_flush_op_results` combined observation (`[i/N] tool — OK/FAILED`,
  any-fail ⇒ failed), turn-ending ops flush accumulated work first.
- **Step 3c** — `md_array` plugin (registered; default since 2026-06-11) with lenient
  terminal parsing (§4.3) and multi-op history records
  (`{thought, ops:[…]}` / `{thought, terminal}`, round-trip via overridden
  serialize/render); the `ready_for_review` termination gate
  (`_terminal_reviewed`, once per run) in `_finish_terminal_turn`.
- **B1 loop detector**: kept per-op `observe` (NOT an op-set signature) — with
  threshold 3, a duplicated op inside one turn doesn't fire, while the same
  (action, args) three times in a row (across turn boundaries or not) does.
  Simpler than the op-set idea and semantically right.

Invariants held: turns.jsonl schema unchanged; react/prefix_md byte-identical
prompts + full-suite green. Shipped opt-in behind `--response-format md_array`;
promoted to `DEFAULT_WIRE_FORMAT` on 2026-06-11 (prefix_md kept as fallback).

---

## 7. Risks

- false-terminate (§4.4 gate).
- Multi-turn degeneration — unmeasured; **a Phase-2 full-loop bakeoff is
  mandatory before adoption** ([prefix_md_full_dropped]: a new wire format must
  bakeoff first).
- Dropped-op-action unrecoverable without the prefix (§4.1).
- Recovery complexity (singular → set).
- 35B terminal edge: trivial-greet termination stays ~40% on 35B even *with*
  lenient parsing (§4.3). The model writes varied free-form "no action" phrasings
  beyond the `none`/`n/a` markers; covering all is whack-a-mole. 27B terminates
  cleanly (100%). A loop-level termination gate (§4.4) or a small dedicated
  terminal affordance may be the durable fix — open.
- ReAct prior: 0% action-leak measured, but only two omlx models.

---

## 8. Decision gate (before §6)

1. Adopt the `ready_for_review` termination gate? (§4.4)
2. Sequential ops first (recommended) or parallel? (§6)
3. Partial-failure → turn success: any-fail ⇒ fail (recommended)? (§6)
4. Invest in format-aware tool guides now, or keep the bakeoff post-processor
   until the format is otherwise locked? (§5)
