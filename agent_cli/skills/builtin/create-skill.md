---
name: create-skill
description: Create a new skill file interactively. Generates SKILL.md with frontmatter, prompt template, and optional scripts. Use when asked to create, make, or add a new skill.
argument-hint: "<skill-name> [description]"
allowed-tools: [read_file, write_file, shell, ask]
disable-model-invocation: true
---

You are a skill builder for agent-cli. Create a new skill based on the user's request.

## Skill file format

agent-cli skills are markdown files with YAML frontmatter:

```markdown
---
name: skill-name
description: What the skill does (1-2 sentences)
allowed-tools: [read_file, write_file, edit_file, shell]
max-iter: 0
argument-hint: "<path>"
model: null
context: null
disable-model-invocation: false
user-invocable: true
---

Prompt template body here. This is the instruction given to the LLM.
Use $ARGUMENTS for user input, $ARGUMENTS[0], $ARGUMENTS[1] for positional args.
Use ${SKILL_DIR} to reference the skill's directory (for scripts/).
Use !`command` for inline shell execution in the prompt.
```

## Frontmatter fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| name | yes | | Skill identifier (used as /command) |
| description | yes | | What the skill does — be specific for auto-invocation |
| allowed-tools | no | all | Tools the skill can use: read_file, write_file, edit_file, shell, ask, delegate |
| max-iter | no | 0 (global) | Max iterations for this skill |
| argument-hint | no | "" | Usage hint shown in /skills list |
| model | no | caller's | Override model for this skill |
| context | no | shared | "fork" for independent context |
| disable-model-invocation | no | false | true = LLM cannot auto-invoke, user only |
| user-invocable | no | true | false = hidden from /skills menu |

## Directory structure (for skills with scripts)

```
.agent-cli/skills/<skill-name>/
├── SKILL.md
└── scripts/
    ├── run.sh
    └── helper.py
```

Reference scripts from the prompt: `!`bash ${SKILL_DIR}/scripts/run.sh``

## Task

1. Parse the skill name from $ARGUMENTS. If not provided, ask the user.
2. Ask the user what the skill should do (unless already described in arguments).
3. Determine:
   - Which tools are needed (read_file, write_file, edit_file, shell, ask, delegate)
   - Whether scripts are needed (if yes, create scripts/ directory)
   - Whether the skill should be model-invocable or user-only
   - Whether it needs independent context (fork)
4. Generate the SKILL.md file with appropriate frontmatter and prompt.
5. If scripts are needed, create them in scripts/ with proper shebang and permissions.
6. Write to `.agent-cli/skills/<name>.md` (flat) or `.agent-cli/skills/<name>/SKILL.md` (with scripts).
7. Verify the file was created correctly by reading it back.

## Writing good prompts

- Start with a clear role: "You are a [role] that [does what]."
- Be specific about inputs, outputs, and format.
- Include constraints and edge cases.
- Use $ARGUMENTS for user-provided input.
- Keep it focused — one skill, one job.
- If the skill produces output files, specify the path and format.

## Writing good descriptions

The description is the ONLY trigger mechanism for auto-invocation. Be specific:
- Bad: "Processes files"
- Good: "Analyze Python source files for code style issues, run linting, and generate a fix report"
