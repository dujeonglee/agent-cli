# Agentic Loop CLI

A command-line interface for executing agentic workflows using the ReAct (Reasoning + Action) pattern with JSON response format.

## Overview

This tool implements an AI agent loop that can solve tasks step-by-step by reasoning about the problem and using available tools. It supports multiple LLM providers and includes tools for file operations, shell commands, and subagent delegation.

## Features

- **ReAct Pattern**: Combines reasoning and action in an iterative loop
- **JSON Response Format**: No tool-call API required - simple JSON structures
- **Multiple LLM Providers**: Anthropic, OpenAI-compatible APIs, and Ollama
- **Tool Support**: read_file, write_file, edit_file, shell, delegate
- **Interactive Chat Mode**: Persistent context with automatic compression
- **Subagent Delegation**: Nested agentic loops with configurable depth limits
- **Rich CLI Interface**: Beautiful terminal output using Rich library

## Supported LLM Providers

| Provider | Default Model | API Key Environment Variable |
|----------|---------------|------------------------------|
| Anthropic | claude-sonnet-4-20250514 | ANTHROPIC_API_KEY |
| OpenAI | gpt-4o | OPENAI_API_KEY |
| Ollama | qwen3:32b | (none - local) |

## Installation

```bash
pip install typer rich requests
```

## Usage

### Basic Command

```bash
python agent-cli.py run <query> [options]
```

### Options

| Option | Description |
|--------|-------------|
| `-p, --provider` | LLM provider: anthropic | openai | ollama |
| `-m, --model` | Model ID (uses provider default if not specified) |
| `--base-url` | API base URL (uses provider default if not specified) |
| `--api-key` | API key (auto-detects from environment if not specified) |
| `-n, --max-iter` | Maximum iterations (0 = unlimited) |
| `--max-depth` | Maximum subagent nesting depth (default: 2) |
| `--delegate-timeout` | Timeout in seconds for subagent delegation (default: 300) |
| `-v, --verbose` | Show raw LLM response |
| `--quiet` | Output only the final answer (used internally by subagents) |

### Examples

#### Basic Task Execution
```bash
# List files in the current directory and read README.md
agent-cli.py run "List files in the current directory and read README.md"

# Create a new file with a hello world program
agent-cli.py run "Create test.py with a hello world" -p ollama -m qwen3:8b

# Use Anthropic with API key
agent-cli.py run "Analyze this project" -p anthropic --api-key sk-ant-...
```

#### Direct Shell Commands
```bash
# Run shell command directly without LLM
agent-cli.py run "/sh ls -la"
agent-cli.py run "/sh python --version"
```

#### vLLM Support
```bash
# vLLM exposes an OpenAI-compatible API
agent-cli.py run "Task here" -p openai --base-url http://localhost:8000/v1 -m your-model
```

### Interactive Chat Mode

```bash
python agent-cli.py chat [options]
```

#### Chat Commands

| Command | Description |
|---------|-------------|
| `/quit` / `/exit` | End the session |
| `/clear` | Reset conversation context |
| `/sh <cmd>` | Run a shell command directly |

## Tool Schema

### read_file
Reads a file and returns content with hashline tags for editing.
```json
{
  "thought": "I need to read the file to understand its structure",
  "action": "read_file",
  "action_input": {
    "path": "file path to read"
  }
}
```

### write_file
Creates or overwrites a file with raw content.
```json
{
  "thought": "I need to create a new file",
  "action": "write_file",
  "action_input": {
    "path": "file path to save",
    "content": "file content"
  }
}
```

### edit_file
Edits a file using hashline references from read_file.
```json
{
  "thought": "I need to modify specific lines in the file",
  "action": "edit_file",
  "action_input": {
    "path": "file path",
    "edits": [
      {
        "op": "replace|append|prepend",
        "pos": "LINE#HASH",
        "end": "LINE#HASH (optional, for range replace)",
        "lines": ["new lines"]
      }
    ]
  }
}
```

### shell
Runs a shell command and returns stdout/stderr.
```json
{
  "thought": "I need to run a shell command",
  "action": "shell",
  "action_input": {
    "command": "shell command to run",
    "timeout": 30
  }
}
```

### delegate
Delegates a self-contained subtask to an independent subagent.
```json
{
  "thought": "This task is complex, I'll delegate it to a subagent",
  "action": "delegate",
  "action_input": {
    "task": "fully self-contained task description"
  }
}
```

## Response Format

Agents must respond with a single JSON object:

### Format A - Use a Tool
```json
{
  "thought": "your reasoning",
  "action": "tool_name",
  "action_input": {...}
}
```

### Format B - Final Answer
```json
{
  "thought": "your reasoning",
  "final_answer": "your complete answer"
}
```

## Hashline Editing

Files read with `read_file` are tagged with hashline references:
```
1#VR:def hello():
2#KT:    return "world"
3#ZZ:
```

To edit, use `edit_file` with hashline refs copied EXACTLY from read_file output:
- **replace single line**: `{"op": "replace", "pos": "2#KT", "lines": ["    return \"hello\""]}`
- **replace range**: `{"op": "replace", "pos": "1#VR", "end": "3#ZZ", "lines": ["def greet():", "    pass"]}`
- **delete lines**: `{"op": "replace", "pos": "2#KT", "lines": []}`
- **append after**: `{"op": "append", "pos": "1#VR", "lines": ["    # new comment"]}`
- **prepend before**: `{"op": "prepend", "pos": "1#VR", "lines": ["# header"]}`
- **append to EOF**: `{"op": "append", "lines": ["# end of file"]}`

## Context Management

- The chat mode maintains conversation history automatically
- When context exceeds the maximum size (default: ~128,000 tokens), older messages are compressed using the LLM
- Keep recent message pairs (default: 4) verbatim while compressing older ones

## Delegation Rules

When delegating tasks to subagents:
- Only delegate tasks that are fully independent and self-contained
- The subagent has NO memory of this conversation
- Include ALL details: file paths, content, specific instructions
- NEVER use pronouns or references to prior context in the task
- **Good**: "Read /tmp/data.csv and count the number of rows"
- **Bad**: "Analyze the file we discussed earlier"

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | API key for Anthropic provider |
| `OPENAI_API_KEY` | API key for OpenAI provider |

## License

MIT License
