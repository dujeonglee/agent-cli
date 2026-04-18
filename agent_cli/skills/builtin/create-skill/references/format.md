# Skill file format reference

This file is read at runtime by the create-skill builder, so every
placeholder below (`${SKILL_DIR}`, `$ARGUMENTS`, `` !`cmd` ``, etc.)
is literal — these strings are exactly what must appear in the
SKILL.md you produce.

## File shape

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
| hooks | no | (none) | Skill-local shell hooks merged on top of the caller's for the duration of this skill's execution. Same YAML shape as `hooks.json` or an agent's `hooks:` block. Use for skill-scoped PreToolUse/PostToolUse policies (audit logging, input rewriting, conditional blocking) that shouldn't apply outside this skill. Skip unless the skill needs a deterministic, tool-level policy beyond what allowed-tools can express. |

### Hook block shape (when `hooks:` is used)

```yaml
hooks:
  PreToolUse:               # or PostToolUse / PostToolUseFailure
    - matcher: shell        # tool name regex; "" matches all
      hooks:
        - command: "echo audit >> /tmp/skill.log"
          timeout: 5        # seconds, optional (default 30)
```

Runtime behaviour:
- stdin receives a JSON payload: `{hook_event_name, tool_name, tool_input[, tool_result]}`.
- exit 0 → allow; exit 2 → block the tool (PreToolUse only); stdout JSON with `updatedInput` → replace the tool's input dict.

## Directory structure (for skills with scripts)

```
.agent-cli/skills/<skill-name>/
├── SKILL.md
└── scripts/
    ├── run.sh
    └── helper.py
```

## Referencing scripts — always use ${SKILL_DIR}

The executor substitutes `${SKILL_DIR}` at runtime with the parent
directory of the skill's SKILL.md. That is the only script-reference
form that keeps working when the skill is copied, moved, or installed
globally. **Never hardcode absolute paths in SKILL.md.**

DO (literal placeholder, resolved at runtime):

```
bash ${SKILL_DIR}/scripts/run.sh
bash ${SKILL_DIR}/scripts/helper.py arg1 arg2
```

DON'T (every one of these is wrong — they are not portable):

```
bash /Users/alice/workspace/proj/.agent-cli/skills/<name>/scripts/run.sh
bash /Users/alice/workspace/proj/agent_cli/skills/builtin/scripts/run.sh
bash /home/alice/.agent-cli/skills/<name>/scripts/run.sh
bash ~/.agent-cli/skills/<name>/scripts/run.sh
```

If you catch yourself typing `/Users/`, `/home/`, `/opt/`, `C:\`, or
`~/` into the script command, stop and replace the whole thing with
`${SKILL_DIR}`.

## Two execution patterns

### Pattern A — runtime invocation via the `shell` tool (most common)

The LLM calls the `shell` tool with the script path during skill
execution. Use this when the script's output should drive the LLM's
subsequent reasoning, or when it produces a lot of text.

Frontmatter: `allowed-tools: [shell]`

SKILL.md body example:

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

SKILL.md body example:

```markdown
Current directory listing:
!`bash ${SKILL_DIR}/scripts/run.sh`

Summarize the above for the user.
```

Caveats: the command runs synchronously every time the skill is
invoked, its stdout is inserted verbatim, and a non-zero exit does not
abort the skill — prefer Pattern A for anything more than a short
pre-computed snippet.
