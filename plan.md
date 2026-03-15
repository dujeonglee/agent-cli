# Implementation Plan: Planning Mode for Agent-CLI

## Current State Analysis

### Existing Features (agent-cli.py)
- ✅ ReAct pattern implementation with `run_loop()` function
- ✅ Multiple LLM provider support (Anthropic, OpenAI, Ollama)
- ✅ Tool system (read_file, write_file, edit_file, shell)
- ✅ ContextManager for conversation history
- ✅ Interactive chat mode
- ✅ Rich CLI interface with rendering helpers
- ✅ Delegate tool for subagent execution
- ✅ SectionReActParser for parsing LLM responses

### Missing Features for Planning Mode
- ❌ Plan generation functionality
- ❌ Plan review/approval workflow
- ❌ Step execution with progress tracking
- ❌ Replanning on failure
- ❌ CLI `plan` command
- ❌ `/plan` chat command
- ❌ Plan data structures (PlanStep, Plan)
- ❌ Plan rendering UI

---

## Implementation Phases

### Phase 1: Core Data Structures & Parser Extension

**Priority: P1 (Foundation)**
**Estimated Effort: Small**

#### Tasks
1. **Create Plan Data Classes**
   ```python
   @dataclass
   class PlanStep:
       id: int
       description: str
       status: str = "pending"
       result: str | None = None
   
   @dataclass
   class Plan:
       goal: str
       steps: list[PlanStep]
       current_step: int = 0
   ```

2. **Parse `>>>PLAN` Section from LLM Response**
   - Extend `SectionReActParser` to recognize `>>>PLAN` keyword
   - Extract numbered steps (1., 1), - step, etc.)
   - Lenient parsing for various formats

3. **Add Plan System Prompt Variant**
   - Create `build_plan_generation_prompt()` function
   - Template for generating step-by-step plans
   - Configurable max_steps parameter

#### Deliverables
- `plan.md` - Data structures defined
- Parser handles `>>>PLAN` section
- Plan generation prompt ready

---

### Phase 2: Basic Plan Command

**Priority: P2 (Visible Feature)**
**Estimated Effort: Small**

#### Tasks
1. **Implement `plan` CLI Command**
   - New Typer command with options:
     - `--auto-approve` - Skip review
     - `--plan-only` - Generate plan only
     - `--max-steps` - Max plan steps (default: 20)
   - Reuse existing provider/model options

2. **Implement Plan Generation Function**
   ```python
   def generate_plan(query, provider, model, ...):
       system = build_plan_generation_prompt()
       llm_response = call_llm(system, user_query)
       plan = parse_plan_from_llm(llm_response)
       return plan
   ```

3. **Add Plan Display Renderer**
   - Create `render_plan(plan)` function using Rich
   - Show steps with [ ] status indicators
   - Clean, readable format

#### Deliverables
- Working `agent plan` command
- Basic plan generation
- Rich plan display

---

### Phase 3: Plan Review Workflow

**Priority: P3 (User Experience)**
**Estimated Effort: Medium**

#### Tasks
1. **Implement Interactive Review**
   - Show plan with Rich panel formatting
   - Prompt: "Approve? [Y]es / [E]dit / [R]egenerate / [N]o"
   - Handle all options:
     - Y: Proceed to execution
     - E: Edit mode
     - R: Regenerate plan
     - N: Cancel

2. **Plan Edit Mode**
   - Allow editing step descriptions
   - Add new steps
   - Remove existing steps
   - Simple text-based editing

3. **Auto-Approve Mode**
   - Skip review when `--auto-approve` is set
   - Directly proceed to execution

#### Deliverables
- Interactive plan review UX
- Edit capabilities
- Auto-approve functionality

---

### Phase 4: Step-by-Step Execution

**Priority: P4 (Core Functionality)**
**Estimated Effort: Medium**

#### Tasks
1. **Implement `execute_plan()` Function**
   - Iterate through plan steps
   - Call `run_loop()` for each step
   - Update step status in real-time

2. **Add Step Context Injection**
   - Inject step context into LLM prompts
   - Include previous step results
   - Show progress: "Step 3 of 5: [description]"

3. **Progress Display**
   - `[ ]` pending
   - `[→]` in progress
   - `[✓]` completed
   - `[✗]` failed
   - `[~]` skipped

4. **Failure Handling**
   - On step failure, present options:
     - [R]etry
     - [S]kip
     - [P]lan remaining
     - [A]bort
   - Update step status with result/error

#### Deliverables
- Working step execution
- Progress tracking display
- Failure recovery flow

---

### Phase 5: Replanning & Chat Integration

**Priority: P5 (Advanced Features)**
**Estimated Effort: Medium**

#### Tasks
1. **Implement Replanning**
   - Generate revised remaining steps after failure
   - Re-show plan for approval
   - Resume from revised plan

2. **Add `/plan` Chat Command**
   - Trigger planning from chat mode
   - Same review workflow
   - Integrate with ContextManager

3. **Plan Persistence Options**
   - Consider future disk storage
   - Design for resumability

#### Deliverables
- Replanning functionality
- `/plan` chat command
- Chat mode integration

---

### Phase 6: Polish & Edge Cases

**Priority: P6 (Quality)**
**Estimated Effort: Small**

#### Tasks
1. **Handle Edge Cases**
   - Empty plan validation
   - Single-step plan warnings
   - Vague step detection
   - Context window management

2. **Quiet Mode Support**
   - For scripting/CI integration
   - Output only final results

3. **Documentation**
   - Update README.md with new features
   - Add examples
   - CLI help text

#### Deliverables
- Edge case handling
- Quiet mode working
- Updated docs

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        CLI Interface                         │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │   agent run  │  │  agent plan  │  │  agent chat (/plan)│  │
│  └──────────────┘  └──────────────┘  └───────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────┐
│                        Plan Generator                        │
│  ┌────────────────────────────────────────────────────────┐ │
│  │   system_prompt = build_plan_generation_prompt()      │ │
│  │   llm_response = call_llm(system, query)              │ │
│  │   plan = parse_plan_from_llm(llm_response)            │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                                   │
                   ┌───────────────┼───────────────┐
                   ▼               ▼               ▼
          ┌────────────────┐ ┌──────────────┐ ┌────────────────┐
          │   Review Mode  │ │ Auto-Approve │ │   Plan-Only    │
          └────────────────┘ └──────────────┘ └────────────────┘
                   │               │               │
                   ▼               ▼               ▼
          ┌─────────────────────────────────────────────────┐   │
          │              execute_plan()                     │   │
          │  ┌───────────────────────────────────────────┐  │   │
          │  │  For each step:                           │  │   │
          │  │  1. Inject step context                   │  │   │
          │  │  2. Call run_loop()                       │  │   │
          │  │  3. Update step status                    │  │   │
          │  │  4. Handle failure/replan                 │  │   │
          │  └───────────────────────────────────────────┘  │   │
          └─────────────────────────────────────────────────┘   │
                                   │                            │
                                   ▼                            │
          ┌───────────────────────────────────────────────────────────┐
          │                     Progress Display                      │
          │  [✓] Step 1 completed  [→] Step 2 in progress             │
          │  [ ] Step 3 pending    [✗] Step 4 failed                  │
          └───────────────────────────────────────────────────────────┘
```

---

## Code Structure

### New Functions to Add

```python
# Data structures
class PlanStep: ...
class Plan: ...

# Plan generation
def build_plan_generation_prompt(max_steps: int) -> str
def generate_plan(
    query: str,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    max_steps: int = 20
) -> Plan | None

def parse_plan_from_llm(text: str) -> Plan | None

# Plan review
def review_plan(plan: Plan, auto_approve: bool = False) -> bool

def edit_plan(plan: Plan) -> Plan

def replan(
    plan: Plan,
    failed_step: int,
    error: str,
    provider: str,
    model: str,
    base_url: str,
    api_key: str
) -> Plan | None

# Plan execution
def execute_plan(
    plan: Plan,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    max_iter: int,
    verbose: bool,
    quiet: bool
) -> str | None

# Rendering
def render_plan(plan: Plan)

def render_plan_progress(plan: Plan)

def render_step_status(step: PlanStep) -> str
```

### Modified Functions

```python
# parser_section.py - Add PLAN support
def parse_section(text: str) -> ReActStep:
    # ... existing code ...
    if keyword == "PLAN":
        plan_steps = extract_plan_steps(content)
        step.plan_steps = plan_steps
    return step

# agent-cli.py - New CLI command
@app.command()
def plan(
    query: str,
    provider: str = "ollama",
    model: Optional[str] = None,
    auto_approve: bool = False,
    plan_only: bool = False,
    max_steps: int = 20,
    ...
):
    plan = generate_plan(...)
    if plan_only or not review_plan(plan, auto_approve):
        render_plan(plan)
        return
    result = execute_plan(plan, ...)
    print(result)
```

---

## Testing Strategy

### Unit Tests
- Plan parsing with various formats
- Plan validation (empty, single step, vague steps)
- Step status transitions

### Integration Tests
- Full plan workflow: generate → review → execute
- Plan editing workflow
- Failure and replan scenarios
- Chat mode `/plan` command

### Manual Testing
- Test with real-world tasks
- Verify progress display accuracy
- Test failure recovery

---

## Open Questions to Address

1. **Plan Persistence** - For v1, keep in-memory only. Future: add `.agent-cli/plans/` directory.

2. **Step Granularity** - Start with LLM-determined granularity. If issues arise, add validation.

3. **Edit UX** - Start with text-based editing (option a). Enhance later if needed.

4. **Model Selection** - For v1, use same model for planning and execution. Add `--plan-model` in future if needed.

5. **Max Iterations per Step** - Each step inherits global `--max-iter`. Consider separate limit for v2.

---

## Estimated Timeline

| Phase | Tasks | Estimated Time |
|-------|-------|---------------:|
| P1 | Data structures, parser, prompt | 2-3 days |
| P2 | Plan command, generation, display | 2-3 days |
| P3 | Review workflow | 3-4 days |
| P4 | Step execution | 3-4 days |
| P5 | Replanning, chat integration | 3-4 days |
| P6 | Edge cases, polish, docs | 2-3 days |
| **Total** | | **15-21 days** |

---

## Success Criteria

- [ ] `agent plan` command works end-to-end
- [ ] Plan generation produces actionable steps
- [ ] Interactive review UX is smooth
- [ ] Step execution shows progress
- [ ] Replanning handles failures gracefully
- [ ] `/plan` chat command integrated
- [ ] All existing features remain functional
- [ ] Documentation is complete

---

*This plan is based on the REQUIREMENTS.md draft and current agent-cli.py implementation.*
