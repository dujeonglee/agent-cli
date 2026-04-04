# Agent Writing Guide for agent-cli

Guide for creating agent definition files at `.agent-cli/agents/{name}.md`.

---

## File Format

```markdown
---
name: agent-name
description: Brief role description (1 sentence)
allowed-tools:
  - read_file
  - shell
---

# Agent Name

You are a [specific role]. Your job is to [what you do].

## Principles
- Principle 1: specific and actionable
- Principle 2: ...
```

## Frontmatter Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| name | no | filename stem | Agent identifier |
| description | no | "" | Role summary |
| allowed-tools | no | all | Tools the agent can use |
| model | no | caller's | Model override |

## Tool Restriction Patterns

| Role | Tools | Reason |
|------|-------|--------|
| Analysis/explore (read-only) | read_file, shell | Prevent modifications; shell for grep/find only |
| Code writer | read_file, write_file, edit_file, shell | Full write access |
| Reviewer (read-only) | read_file, shell | Read code + run tests only |
| Doc writer | read_file, write_file, edit_file, shell | Needs to edit docs |

## Principles for Good Agent Definitions

1. **Clear identity**: Start with "You are a [role]". Vague identity = agent wanders off scope.
2. **Specific principles**: "Be thorough" ✗ → "Be specific: include file path, line number, issue description" ✓
3. **Tool restriction = role match**: Giving write_file to a reviewer means it might edit code instead of reviewing.
4. **Under 30 lines**: Agent body goes into the system prompt — keep it lean.
5. **Explicit shell constraints**: For read-only agents with shell access, state "shell is for search commands only (grep, find, wc)."

## Usage

Via delegate tool with agent parameter:

```json
{"tasks": [{"task": "Review this code", "agent": "code-reviewer", "context": "fork"}]}
```

Via CLI shorthand:

```
@code-reviewer review agent_cli/loop.py
```

## Search Path (priority order)

1. `.agent-cli/agents/` (project, highest)
2. `~/.agent-cli/agents/` (user global)
3. `agent_cli/agents/builtin/` (package built-in, lowest)
