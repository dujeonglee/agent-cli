# Agent Team Design Patterns for agent-cli

Architecture patterns for composing agent teams in agent-cli.

---

## Execution Model

agent-cli teams are built on the **delegate tool**:

```
Orchestrator skill (leader)
  ├─ delegate(agent="A", task="...")  → collect result    ← sequential
  ├─ delegate(tasks=[A, B])           → parallel run      ← parallel
  └─ aggregate results → complete
```

- Leader (orchestrator skill) has central control
- No direct inter-agent communication — leader relays results
- context: "fork" shares parent conversation with agent

## Pattern 1: Pipeline (sequential)

```
A → B → C
```

Include A's result in B's task description. Each step depends on the previous.

**When to use**: design → implement → review workflows
**Implementation**: sequential delegate calls

```json
Step 1: {"tasks": [{"task": "Write design docs", "agent": "architect"}]}
Step 2: {"tasks": [{"task": "Implement per design at docs/...", "agent": "implementer"}]}
Step 3: {"tasks": [{"task": "Review the implementation", "agent": "reviewer"}]}
```

## Pattern 2: Fan-out / Fan-in (parallel)

```
        ┌─ A ─┐
Leader ─┤     ├─ Leader (aggregate)
        └─ B ─┘
```

Run independent tasks simultaneously, merge results.

**When to use**: multi-file analysis, review + docs in parallel
**Implementation**: tasks array with 2+ items

```json
{"tasks": [
    {"task": "Security review", "agent": "reviewer"},
    {"task": "Update documentation", "agent": "doc-keeper"}
]}
```

**Constraint**: inherit mode cannot be used with parallel tasks.

## Pattern 3: Producer-Reviewer (generate-validate)

```
Producer → Reviewer → (if fixes needed) → Producer → Reviewer
```

When quality matters. Maximum 2 retry iterations.

**When to use**: code generation followed by quality validation
**Implementation**: orchestrator checks reviewer result and re-runs if needed

## Combining Patterns

In practice, combine patterns:

```
Pipeline + Fan-out:
  Architect → Implementer → [Reviewer + Doc-Keeper] (parallel)

Pipeline + Producer-Reviewer:
  Implementer → Reviewer → (FAIL?) → Implementer → Reviewer → done
```

## Agent Count Guide

| Task scale | Agents | Tasks per agent |
|-----------|--------|----------------|
| Small (3-5 tasks) | 2-3 | 2-3 |
| Medium (5-10 tasks) | 3-4 | 2-3 |
| Large (10+ tasks) | 4-6 | 2-3 |

More agents = more delegate calls = more time. 3 agents is often optimal.

## Selection Guide

1. Tasks have sequential dependencies → Pipeline
2. Tasks are independent → Fan-out
3. Quality validation needed → add Producer-Reviewer
4. Complex → combine patterns
