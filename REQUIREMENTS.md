# Requirements: Planning Mode for Agent-CLI

> Status: **DRAFT** — pending review
> Date: 2026-03-15

## 1. Background

Agent-CLI currently implements the ReAct (Reasoning + Acting) pattern where the LLM reasons and acts one step at a time. This works well for simple tasks but has limitations for complex, multi-step tasks:

- **No upfront planning** — the agent dives into execution immediately, sometimes going down wrong paths
- **No user review before execution** — users can't approve or modify the approach
- **No progress tracking** — hard to know how far along a multi-step task is
- **Replanning is implicit** — when something fails, the agent retries ad-hoc rather than revising a structured plan

### Research References

Studied the following open-source coding agent projects:

| Project | Key Takeaway |
|---------|-------------|
| [Plandex](https://github.com/plandex-ai/plandex) | Plan → Execute → Debug pipeline, sandbox diff review, step-by-step control |
| [QuantaLogic](https://github.com/quantalogic/quantalogic) | ReAct + CodeAct + Flow YAML workflows, modular tool system |
| [Agent Loop](https://github.com/AlessandroAnnini/agent-loop) | Iteration limits, completion detection, human-in-the-loop `--safe` flag |
| [Goose](https://github.com/block/goose) | Full autonomy with MCP tool integration, CLI + desktop |
| [Plandex-lite](https://github.com/Magnus0969/Plandex-lite) | Multi-agent: Planner / Coder / Architect / Reviewer / Summarizer |

---

## 2. Goals

1. Add a **Planning mode** that generates a step-by-step plan before execution
2. Let users **review, edit, and approve** the plan before any tool is called
3. **Track progress** visually as each step completes
4. Support **replanning** when a step fails or context changes
5. Integrate cleanly with the existing ReAct loop, parsers, and CLI structure

### Non-Goals

- Multi-agent orchestration (Planner/Coder/Reviewer as separate agents) — keep it single-agent
- Sandbox/diff review system (Plandex-style) — out of scope for v1
- New tool definitions — reuse existing tools (read_file, write_file, edit_file, shell, delegate)

---

## 3. User Experience

### 3.1 CLI Interface

**Single-shot plan mode:**
```bash
# Generate plan, prompt for approval, then execute
agent plan "Refactor auth module to use JWT tokens"

# Auto-approve (skip review, execute immediately)
agent plan "Add unit tests for utils.py" --auto-approve

# Plan-only (generate plan, don't execute)
agent plan "Migrate database schema" --plan-only
```

**Chat mode integration:**
```
You: /plan Add error handling to all API endpoints
Agent: [generates and displays plan]
Agent: Approve this plan? [Y]es / [E]dit / [R]egenerate / [N]o
You: y
Agent: [executes step by step]
```

**CLI Options (new `plan` command):**

| Option | Default | Description |
|--------|---------|-------------|
| `--auto-approve` | `false` | Skip review, execute immediately |
| `--plan-only` | `false` | Generate plan and display, don't execute |
| `--max-steps` | `20` | Maximum number of steps in a plan |
| `-p, --provider` | `ollama` | LLM provider (inherited from existing) |
| `-m, --model` | provider default | Model ID (inherited from existing) |
| Other existing options | ... | Same as `run` command |

### 3.2 Plan Display

```
╔══════════════════════════════════════════════════╗
║              PLAN · 5 steps                      ║
╚══════════════════════════════════════════════════╝

  1. [ ] Read current auth module (src/auth.py)
  2. [ ] Analyze existing session-based auth logic
  3. [ ] Install PyJWT dependency via shell
  4. [ ] Rewrite auth.py with JWT token implementation
  5. [ ] Create test_auth.py with unit tests

  Approve? [Y]es / [E]dit / [R]egenerate / [N]o
```

### 3.3 Execution Progress

```
  1. [✓] Read current auth module (src/auth.py)
  2. [✓] Analyze existing session-based auth logic
  3. [→] Install PyJWT dependency via shell
  4. [ ] Rewrite auth.py with JWT token implementation
  5. [ ] Create test_auth.py with unit tests
```

Step status indicators:
- `[ ]` — pending
- `[→]` — in progress
- `[✓]` — completed
- `[✗]` — failed
- `[~]` — skipped (after replan)

---

## 4. Architecture

### 4.1 Plan Data Structure

```python
@dataclass
class PlanStep:
    id: int                          # 1-based step number
    description: str                 # human-readable step description
    status: str = "pending"          # pending | in_progress | done | failed | skipped
    result: str | None = None        # execution result or error message

@dataclass
class Plan:
    goal: str                        # original user query
    steps: list[PlanStep]            # ordered steps
    current_step: int = 0            # index of current step (0-based)
```

### 4.2 Three-Phase Flow

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   PHASE 1   │────▶│   PHASE 2   │────▶│   PHASE 3   │
│  Generate   │     │   Review    │     │   Execute   │
│   Plan      │     │   Plan      │     │   Plan      │
└─────────────┘     └─────────────┘     └─────────────┘
       │                   │                    │
  LLM generates      User approves/       ReAct loop per
  step list from      edits/rejects        step with plan
  the goal            the plan             context injected
```

**Phase 1 — Plan Generation:**
- Send user's goal to LLM with a plan-generation system prompt
- LLM returns a numbered list of steps (parsed via section markers)
- New section keyword: `>>>PLAN`

**Phase 2 — Plan Review (interactive):**
- Display plan with Rich formatting
- User can: approve (Y), edit (E), regenerate (R), cancel (N)
- Edit mode: user specifies step numbers to modify, add, or remove
- Skipped if `--auto-approve` is set

**Phase 3 — Plan Execution:**
- For each step, inject step context into the system prompt:
  `"You are executing step {n}/{total}: {description}"`
- Run the existing `run_loop()` for each step
- Collect result, update step status
- On failure: prompt user to retry / skip / replan remaining steps

### 4.3 Parser Extension

Add `PLAN` as a new section keyword in `SectionReActParser`:

```
>>>PLAN
1. Read current auth module (src/auth.py)
2. Analyze existing session-based auth logic
3. Install PyJWT dependency via shell
4. Rewrite auth.py with JWT token implementation
5. Create test_auth.py with unit tests
```

The parser extracts numbered lines into `PlanStep` objects. Parsing is lenient — accepts `1.`, `1)`, `- `, or bare numbered lines.

### 4.4 System Prompt (Plan Generation)

A separate system prompt variant for Phase 1:

```
You are an AI assistant that creates step-by-step execution plans.

Given a goal, produce a numbered plan of concrete, actionable steps.
Each step should be a single tool action (read file, write file, edit, shell command)
or an analysis/reasoning step.

>>>PLAN
1. Description of step 1
2. Description of step 2
...

Rules:
- Each step should be independently executable
- Steps should be ordered by dependency
- Be specific about file paths and operations
- Keep steps atomic — one action per step
- Include verification steps where appropriate (e.g., run tests)
- Maximum {max_steps} steps
```

### 4.5 Step Execution Prompt

Each step is executed by calling `run_loop()` with a modified query:

```
CONTEXT: You are executing a plan to: "{original_goal}"

Previous steps completed:
  1. [✓] Read current auth module — found session-based auth in src/auth.py
  2. [✓] Analyzed auth logic — uses cookies, no token refresh

Current step (3 of 5): Install PyJWT dependency via shell

Execute this step now. Use tools as needed.
```

This gives the LLM:
- The overall goal for context
- Summary of prior step results for continuity
- The specific step to execute now

### 4.6 Replanning

When a step fails, the agent can replan:

```
Step 3 failed: pip install PyJWT returned error (no internet access)

Options: [R]etry / [S]kip / [P]lan remaining / [A]bort
```

If user chooses **Replan**:
- Send remaining steps + failure context to LLM
- LLM generates revised remaining steps
- User reviews new plan (Phase 2 again)
- Execution continues from the revised plan

---

## 5. Integration Points

### 5.1 Existing Code Reuse

| Component | How it's used in Planning Mode |
|-----------|-------------------------------|
| `run_loop()` | Executes each plan step as a mini ReAct loop |
| `SectionReActParser` | Extended with `>>>PLAN` keyword |
| `ContextManager` | Carries context across steps in chat mode |
| `build_system_prompt()` | New variant for plan generation |
| `render_*()` helpers | New `render_plan()` and `render_plan_progress()` |
| `TOOLS` dict | Unchanged — same tools available |
| Skills system | `/plan` becomes a chat command, not a skill |

### 5.2 New Components

| Component | Description |
|-----------|-------------|
| `PlanStep` / `Plan` dataclasses | Plan data model |
| `generate_plan()` | Phase 1 — calls LLM to produce a plan |
| `review_plan()` | Phase 2 — interactive review with Rich UI |
| `execute_plan()` | Phase 3 — iterates steps, calls `run_loop()` per step |
| `replan()` | Generates revised remaining steps after failure |
| `render_plan()` | Rich display of plan steps with status |
| `plan` CLI command | New Typer command |
| `/plan` chat command | In-chat planning trigger |
| `PLAN_SYSTEM_PROMPT` | System prompt for plan generation |

### 5.3 File Changes

| File | Change |
|------|--------|
| `agent-cli.py` | Add `plan` command, `execute_plan()`, plan rendering, `/plan` in chat |
| `parser_section.py` | Add `PLAN` to `VALID_KEYWORDS`, add `plan_steps` field to parser output |
| `parser_json.py` | Add `plan_steps` field to `ReActStep` dataclass |

---

## 6. Edge Cases & Constraints

1. **Empty plan** — LLM generates 0 steps → show error, ask user to rephrase
2. **Single-step plan** — valid, but suggest using `run` instead
3. **Step too vague** — validation: each step should contain a verb + target
4. **Circular dependency** — not detected (steps are linear); out of scope
5. **Token budget** — plan generation + all step summaries must fit in context window; compress prior step results if needed
6. **Subagent in plan** — delegate tool works within individual steps, same depth limits apply
7. **Chat mode context** — plan + step results integrate with ContextManager for follow-up questions
8. **Quiet mode** — `--quiet` outputs only final results of all steps (for scripting)

---

## 7. Open Questions

> To discuss before implementation:

1. **Plan persistence** — Should plans be saved to disk (e.g., `.agent-cli/plans/`) for resuming later? Or keep in-memory only for v1?

2. **Step granularity** — Should the LLM decide granularity freely, or should we enforce "one tool call per step"? Flexible is more natural but harder to track.

3. **Edit UX** — For plan editing (Phase 2), should we use:
   - (a) Simple text: user types new step text by number
   - (b) Open `$EDITOR` with the plan as a numbered list
   - (c) Inline Rich prompts per step

4. **Model selection** — Should plan generation use a different (cheaper/faster) model than execution? e.g., `--plan-model` option?

5. **Max iterations per step** — Should each step have its own `--max-iter` limit, or share the global one? A step that takes 10+ iterations is likely stuck.

---

## 8. Implementation Order

Suggested phased approach:

| Phase | Scope | Effort |
|-------|-------|--------|
| **P1** | `Plan`/`PlanStep` data model, plan generation prompt, `>>>PLAN` parser | Small |
| **P2** | `plan` CLI command with `--plan-only`, Rich plan display | Small |
| **P3** | Plan review UX (approve/reject/regenerate) | Medium |
| **P4** | Step-by-step execution with progress display | Medium |
| **P5** | Replan on failure, `/plan` chat integration | Medium |
| **P6** | Edit plan UX, `--auto-approve`, polish | Small |
