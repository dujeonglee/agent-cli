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
max-turns: 0
argument-hint: "<path>"
model: null
context: null
disable-model-invocation: false
user-invocable: true
---

Prompt template body here. This is the instruction given to the LLM.
Use $ARGUMENTS for user input, $ARGUMENTS[0], $ARGUMENTS[1] for positional args.
Use ${SKILL_DIR} to reference the skill's directory (for scripts/).
  (${CLAUDE_SKILL_DIR} is also accepted as an alias for Claude Code compatibility.)
Use ${SESSION_ID} for the current session ID.
Use !`command` for inline shell execution at template-render time.
```

## Frontmatter fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| name | yes | | Skill identifier (used as /command) |
| description | yes | | What the skill does — be specific for auto-invocation |
| allowed-tools | no | all | Tools the skill can use: read_file, write_file, edit_file, shell, ask, delegate |
| max-turns | no | 0 (global) | Max iterations for this skill |
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

## Referencing scripts — always use ${SKILL_DIR}

When the skill ships a script, the SKILL.md body MUST reference it through
the `${SKILL_DIR}` placeholder. The executor substitutes `${SKILL_DIR}` at
runtime with the absolute path to the skill's own directory (the parent
of its SKILL.md), so the script is located correctly regardless of where
the skill lives (`.agent-cli/skills/...`, `~/.agent-cli/skills/...`, or
the built-in path).

**Rule — NEVER hardcode absolute paths in SKILL.md.** Do not write
`/Users/...` or `/home/...` even if you know the current path. The skill
may be copied, moved, or installed globally — hardcoded paths break
silently. If you catch yourself inferring a path, stop and use
`${SKILL_DIR}` instead.

Two execution patterns exist — pick based on when the script should run:

### Pattern A — runtime invocation via the `shell` tool (most common)

The LLM calls the `shell` tool with the script path during skill
execution. Use this when the script's output should drive the LLM's
subsequent reasoning, or when it produces a lot of text.

Frontmatter: `allowed-tools: [shell]`

SKILL.md body (example):
```markdown
## Task
Run the helper script to collect the data, then format the results.

Invoke: `shell` with command `bash ${SKILL_DIR}/scripts/run.sh` and
report its output as a readable table.
```

### Pattern B — render-time inline execution with `` !`cmd` ``

The `` !`command` `` syntax runs `command` while the skill's prompt is
being built and splices the stdout into the template. Use this when you
want the script output baked into the prompt the LLM sees (e.g. a small
preamble like "current git branch" or "file count").

SKILL.md body (example):
```markdown
Current directory listing:
!`bash ${SKILL_DIR}/scripts/run.sh`

Summarize the above for the user.
```

Caveats: the command runs synchronously every time the skill is
invoked, its stdout is inserted verbatim, and a non-zero exit does not
abort the skill — prefer Pattern A for anything more than a short
pre-computed snippet.

## Task

1. The first word of $ARGUMENTS is the skill name. The rest is the description.
   If $ARGUMENTS is empty, ask for the name.
2. Bundle ALL clarifying questions into ONE `ask` call (use the `questions` array).
   Do not issue sequential `ask` calls — ask everything at once:
   - What should the skill do? (unless already clear from $ARGUMENTS)
   - Which tools does it need? (read_file, write_file, edit_file, shell, ask, delegate)
   - Does it need shell scripts? (if yes, we'll create scripts/ directory)
   - Should it be model-invocable or user-only?
   - Does it need independent context (fork) or share the caller's?
3. Map user answers to frontmatter fields:
   - tools needed → `allowed-tools: [...]`
   - user-only → `disable-model-invocation: true`
   - independent context → `context: fork`
   - scripts needed → create scripts/ directory (use subdirectory layout)
4. Generate the SKILL.md file with appropriate frontmatter and prompt.
5. If scripts are needed, create them in scripts/ with proper shebang and permissions.
   - Script references in SKILL.md MUST use `${SKILL_DIR}/scripts/<file>`.
   - Do not write absolute paths like `/Users/...` — they are not portable
     and will break when the skill is moved or installed elsewhere.
   - Choose between Pattern A (`shell` tool at runtime) and Pattern B
     (`` !`cmd` `` at render time) per the "Referencing scripts" section.
6. Write to `.agent-cli/skills/<name>.md` (flat) or `.agent-cli/skills/<name>/SKILL.md` (with scripts).
7. Verify the file was created correctly by reading it back. If any
   absolute path slipped into the body, rewrite it using `${SKILL_DIR}`.

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
