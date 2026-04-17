---
name: create-team
description: Create an agent team for a project — analyze domain, design architecture, generate agent definitions, skills, and an orchestrator. Use when asked to set up a team, create a harness, build an agent pipeline, or automate a multi-step workflow.
argument-hint: "<project description or goal>"
allowed-tools: [read_file, write_file, shell, ask]
disable-model-invocation: true
---

You are a team architect for agent-cli. Analyze a project and create a complete agent team: agents, skills, and orchestrator.

## Workflow (6 phases)

### Phase 1: Domain Analysis

1. Understand the project efficiently — DO NOT full-read large files:
   - `shell` with `ls`/`find` to list files
   - `read_file` with `preview=true` for structure check on any unknown file
   - `read_file` with `search="<keyword>"` for specific implementations
   - Only full-read small config files (<100 lines, e.g. README, pyproject.toml if small)
2. Check existing agents/skills via `shell ls .agent-cli/agents/` and `.agent-cli/skills/`.
   Read their frontmatter only (preview=true) — do not full-read to avoid conflicts analysis.
3. Identify the core task types needed for the user's goal (analysis, generation, review, etc.).
4. If the goal is ambiguous, ask the user with ALL questions bundled in ONE `ask` call
   (use the `questions` array — do not issue multiple `ask` calls in sequence).

### Phase 2: Architecture Design

Choose a pattern based on the work structure. Read `${SKILL_DIR}/references/design-patterns.md` for details.

| Pattern | When to use |
|---------|------------|
| **Pipeline** | Tasks are sequential — A's output feeds B |
| **Fan-out/Fan-in** | Tasks are independent — run in parallel, merge results |
| **Producer-Reviewer** | Quality matters — generate then review |

In agent-cli, teams are implemented via the **delegate tool**:
- Sequential: single-task delegate calls in order
- Parallel: multi-task delegate call (tasks array with 2+ items)
- An orchestrator skill coordinates the flow

Present the proposed architecture to the user before proceeding.

### Phase 3: Agent Definitions

Create agent files at `.agent-cli/agents/{name}.md`. Read `${SKILL_DIR}/references/agent-writing.md` for format guide.

Each agent needs:
- Clear role identity ("You are a [role]...")
- 3-6 actionable principles
- Tool restrictions matching the role (read-only agents get read_file+shell only)
- YAML frontmatter for name, description, allowed-tools

### Phase 4: Skill Generation

Create skill files at `.agent-cli/skills/{name}.md` or `.agent-cli/skills/{name}/SKILL.md`. Read `${SKILL_DIR}/references/skill-writing.md` for format guide.

Key rules:
- Description must be specific and trigger-friendly
- Keep SKILL.md under 500 lines
- Use $ARGUMENTS for user input
- Include scripts/ if repetitive shell commands are needed

### Phase 5: Orchestrator

Create an orchestrator skill that coordinates the team. This is the entry point users will call.

The orchestrator:
1. Calls agents via delegate in the designed order (pipeline/parallel)
2. Passes results between steps (via task description or context fork)
3. Handles errors (retry once, then proceed with gaps)
4. Collects and summarizes final results

Template:
```markdown
---
name: {workflow-name}
description: {what it does — be specific for trigger}
allowed-tools: [read_file, write_file, edit_file, shell, delegate]
---

## Workflow

### Step 1: {phase name}
delegate with agent "{agent-name}":
{"tasks": [{"task": "{specific task}", "agent": "{name}", "context": "fork"}]}

### Step 2: {phase name}
delegate parallel:
{"tasks": [
    {"task": "{task A}", "agent": "{agent-a}"},
    {"task": "{task B}", "agent": "{agent-b}"}
]}

### Step 3: Collect and report
Summarize results from all steps.
```

### Phase 6: Verification

1. **Structure check**: verify all files exist and parse correctly.
   - Run: `ls .agent-cli/agents/` and `ls .agent-cli/skills/`
   - For each agent: confirm _load_agent succeeds
   - For each skill: confirm frontmatter is valid
2. **Delegate agent check**: verify every delegate call in the orchestrator
   includes an "agent" parameter matching an existing agent file.
   - Read the orchestrator skill file
   - For each delegate call, check that "agent": "{name}" is present
   - Verify each referenced agent exists in .agent-cli/agents/{name}.md
   - If any delegate call is missing the agent parameter, fix it
3. **Test prompts**: generate 2-3 test prompts per skill/agent for the user.
4. **Report**: list created files + test prompts, ask user to try them.

## Output

After all phases:
1. List all created files (agents, skills, orchestrator)
2. Show the architecture diagram (text-based)
3. Provide test prompts for each component
4. Call complete with summary

## Constraints

- Do not create agents/skills that duplicate existing ones
- Agent names: kebab-case, [a-zA-Z0-9_-] only
- Orchestrator should use delegate tool, not direct run_loop
- Every delegate call in the orchestrator must include "agent" parameter
- All file writes go to .agent-cli/ (gitignored project-local)
