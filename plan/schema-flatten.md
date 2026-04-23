# Plan: ReAct Schema Flatten (Phase 2)

> Status: **Deferred**. Phase 1 (lenient parser / two-layer hoist) landed 2026-04-23. Flatten is the follow-up that promotes the flat form from "tolerated drift" to "canonical schema".

## Context

Phase 1 shipped `_normalize_action_input()` in `agent_cli/parsing/react_parser.py`: a two-layer normalizer that accepts both forms:

```json
// Nested (current canonical, system prompt teaches this)
{"thought":"...", "action":"shell", "action_input":{"command":"ls"}}

// Flat (drift observed across multiple model families; now silently normalized)
{"thought":"...", "action":"shell", "command":"ls"}
```

The parser no longer cares which form the model emits. The downstream `action_input` dict is identical.

This plan is about the next step: **changing the system prompt, examples, docs, and tests to make the flat form the primary/canonical one**. The parser itself needs no further work for this move.

## Why flatten

1. **Models naturally emit flat.** The drift we kept firefighting — qwen3 emitting `result` as sibling of `action` for `complete`, qwen3.6 emitting `command` as sibling for `shell` — suggests the nested `action_input` wrapper is artificial for LLMs. Training corpora (ReAct-style traces, chat-completion style) mostly use flat.
2. **Simpler canonical form.** One fewer level of nesting. Human readers of `history.jsonl` parse it faster.
3. **Less parser indirection.** Layer 2 of `_normalize_action_input` already does the flat-form work; the only reason it's called a "hoist" is framing. If flat is canonical, it's just the main path.
4. **Stays backward compatible.** Because Phase 1 keeps accepting nested, existing models tuned to nested continue to work. No cold-switch required.

## What Phase 2 changes

**No parser changes.** Phase 1's `_normalize_action_input` already handles both. This is purely a re-framing + prompt/doc rewrite.

### System prompt (`agent_cli/prompts/system_prompt.py`)

- `FORMAT_RULES` section: canonical examples switch to flat form.
  ```
  {"thought": "...", "action": "shell", "command": "ls"}
  {"thought": "...", "action": "complete", "result": "..."}
  ```
- Rule set reference to `action_input` (currently rule #2: "`action_input` must match the tool's input schema") rewritten to: "tool arguments appear as sibling keys of `action`, matching the tool's input schema."
- A tolerance line: "the older nested `action_input` form is also accepted but not preferred."

### Inline tool guides (`_HASHLINE_INLINE`, `_DELEGATE_INLINE`, `_READ_FILE_INLINE`)

- Update every JSON example in these sections to the flat form.
- Keep the semantic content intact (hashline refs, delegate spec, etc.) — only the wrapper changes.

### Virtual-tool alias handling

Currently the alias mapping (`answer` → `result`, etc.) lives in the parser's Layer 1 (`_VIRTUAL_TOOL_PAYLOAD_HOIST`). In a flat-canonical world the aliases are *just top-level keys*, and the parser's Layer 2 would pick them up verbatim. We need to **move alias handling to the consumer side** (`loop.py`):

```python
# loop.py, complete handler
if parsed.action == "complete":
    ai = parsed.action_input or {}
    answer = (
        ai.get("result")
        or ai.get("answer")
        or ai.get("response")
        or ai.get("final")
        or ai.get("output")
        or "(Completed without result ...)"
    )
```

After that, Layer 1 of `_normalize_action_input` can be collapsed — virtual tools fall through to Layer 2 like any other action. `_VIRTUAL_TOOL_PAYLOAD_HOIST` is deleted.

### Tests

- Existing `TestVirtualToolPayloadHoist` tests: rewrite to the flat canonical. Keep a handful as backward-compat regression (prove nested still parses).
- `TestRealToolArgHoist`: keep; these become the "main path" tests after flatten.
- New tests in `test_loop.py` for the consumer-side alias handling.

### Documentation

- `docs/ARCHITECTURE.md` §5.3: rewrite the "형제 키 정규화" section. The two-layer description goes away; it becomes "flat is canonical; nested is accepted as legacy".
- `README.md`: wherever ReAct JSON examples appear, flip to flat.
- This plan file: mark Status as Completed, or archive.

## Trade-offs

### Pros
- Parser logic matches prompt guidance (no more "model drifts from what we teach").
- Drift reduction: the most frequent drift pattern becomes the expected pattern.
- Less Python in the parser; fewer comments explaining "why we hoist".

### Cons
- Large doc + prompt churn for a refactor that produces no new functionality.
- Risk of breaking models that were trained on nested ReAct traces — though Phase 1's backward-compat parser softens this.
- The alias mapping (answer → result) becomes `loop.py`'s problem. Parser layering gets slightly simpler, consumer slightly more complex.
- Test churn: dozens of tests with `action_input={"path":"a.py"}` need decisions: keep as nested-form coverage, or flip to flat?

## Decision checklist (when to execute)

Execute Phase 2 only after we've answered these with evidence from real sessions:

- [ ] **Phase 1 stable?** No regressions observed for ≥2 weeks of real use.
- [ ] **Drift still frequent?** If we grep `history.jsonl` across sessions, are sibling-form emissions still >10% of turns? If yes, the nested form is working against us — flatten is earned. If <5%, Phase 1 is enough and we don't need the churn.
- [ ] **Prompt rewrite planned?** System prompt rewrites are high-cost; batch this with other prompt work if possible.
- [ ] **Model coverage?** Test flat canonical against the provider matrix we care about: qwen3 family (Ollama), Claude (Anthropic), gpt-4o (OpenAI). All three should handle flat form correctly after the prompt change.

## Not in scope

- **XML migration** — considered earlier in session 2026-04-23, dismissed. See conversation log.
- **Native tool-use API** — architectural choice to stay on ReAct text parsing.
- **Multi-action-per-turn** — orthogonal feature; flat vs nested doesn't determine it.

## Signal to gather before deciding

Add a debug log line inside `_normalize_action_input` Layer 2 (non-virtual hoist path) so we can grep real sessions:

```python
# After the extras bundling that triggers a sibling hoist:
debug_log(f"[hoist] sibling args for action={result.action}, keys={list(extras)}")
```

Run under `AGENT_CLI_VERBOSE=1` for a week of typical use; grep the stderr log for `[hoist] sibling args`. If the ratio is high, flatten is justified.

---

Written: 2026-04-23. Review quarterly; close if Phase 1 turns out to be enough.
