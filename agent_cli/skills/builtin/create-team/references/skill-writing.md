# Skill Writing Guide for agent-cli

Guide for creating skill files at `.agent-cli/skills/{name}.md` or `.agent-cli/skills/{name}/SKILL.md`.

---

## File Format

```markdown
---
name: skill-name
description: What this skill does — be specific for auto-trigger
allowed-tools: [read_file, write_file, shell]
max-turns: 0
argument-hint: "<path>"
---

Prompt template body. Use $ARGUMENTS for user input.
```

## Frontmatter Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| name | yes | | Skill identifier (/command name) |
| description | yes | | Skill description (trigger mechanism) |
| allowed-tools | no | all | Tools the skill can use |
| max-turns | no | 0 (global) | Max iterations |
| argument-hint | no | "" | Usage hint shown in /skills |
| model | no | caller's | Model override |
| context | no | shared | "fork" for independent context |
| disable-model-invocation | no | false | true = LLM cannot auto-invoke |
| user-invocable | no | true | false = hidden from /skills menu |

## Variable Substitution

| Variable | Description |
|----------|-------------|
| `$ARGUMENTS` | Full user input |
| `$0`, `$1`, `$ARGUMENTS[N]` | Nth argument (0-indexed) |
| `${SKILL_DIR}` | Skill directory path |
| `${SESSION_ID}` | Current session ID |
| `` !`command` `` | Inline shell execution |

## Description Writing — Trigger is Everything

The description is the only auto-trigger mechanism for skills.

Bad: `"Processes files"`
Good: `"Read Python source files and analyze for code style, bugs, and security issues. Generate a prioritized report. Use this skill when asked for code review, code analysis, or code inspection."`

Principles:
1. State what the skill does + specific trigger situations
2. Be slightly "pushy" — compensate for LLM's conservative trigger tendency

## Prompt Writing Principles

1. **Role first**: "You are a [role] that [does what]."
2. **Explain why**: Instead of "ALWAYS/NEVER", explain the reasoning. LLMs handle edge cases better when they understand the rationale.
3. **Keep it lean**: Under 500 lines. Do not write what the LLM already knows.
4. **Generalize**: Instead of narrow rules for specific examples, explain the principle.
5. **Imperative tone**: Use directive language.

## Directory Structure (when scripts are needed)

```
.agent-cli/skills/my-skill/
├── SKILL.md
├── scripts/
│   ├── lint.sh
│   └── validate.py
└── references/
    └── api-docs.md
```

Reference scripts from prompt: `` !`bash ${SKILL_DIR}/scripts/lint.sh` ``

## Script Bundling Criteria

Bundle into scripts/ when agents repeatedly generate the same code:
- Same helper script generated 3+ times → bundle it
- Same pip/npm install every run → document dependency step in skill
- Same error workaround repeatedly → document as known issue
