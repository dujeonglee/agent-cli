# Optimization ToDo

> Analyzed: ./
> Date: 2025-03-22

## High Priority
- [ ] [Time & Space Complexity] Inefficient repeated operations in `_detect_runtime_capabilities` function — agent_cli/providers/compat.py:263-380 — Cache runtime capabilities per provider/model combination to avoid repeated API calls during model capability detection
- [ ] [Error Handling] Overly broad exception handling in `run_loop` — agent_cli/loop.py:105-109 — Catch specific exceptions instead of generic `Exception`, log the error with context
- [ ] [Error Handling] Bare `except Exception` in `_compress` method — agent_cli/context/manager.py:132 — Add specific exception handling with actionable error messages
- [ ] [Error Handling] Bare `except Exception` in `tool_delegate` — agent_cli/tools/delegate.py:73 — Add specific exception handling for subprocess operations
- [ ] [Time & Space Complexity] Redundant JSON parsing in `_format_openai_tool_messages` — agent_cli/loop.py:390-415 — Cache parsed JSON when possible, avoid redundant conversions
- [ ] [Readability] Large `run` function in main.py — agent_cli/main.py:144-400+ — Extract command handler logic into separate functions
- [ ] [Readability] Large `run_loop` function in loop.py — agent_cli/loop.py:23-300+ — Decompose into smaller, focused functions for each phase

## Medium Priority
- [ ] [Time & Space Complexity] Repeated regex compilation in `_detect_runtime_capabilities` — agent_cli/providers/compat.py:263-380 — Compile regex patterns once at module level
- [ ] [Time & Space Complexity] Inefficient file reading in `_parse_skill_file` — agent_cli/skills/loader.py:69-117 — Consider caching file contents for repeated access
- [ ] [Readability] Complex `_validate_tool_input` function — agent_cli/tools/registry.py:224-285 — Break into smaller validation functions
- [ ] [Readability] Deeply nested conditionals in `_detect_runtime_capabilities` — agent_cli/providers/compat.py:263-380 — Flatten nested logic with early returns
- [ ] [Duplicate Code] Similar error handling patterns in provider modules — agent_cli/providers/*.py — Create a shared error handling utility
- [ ] [Error Handling] Missing timeout handling in API calls — agent_cli/providers/*.py — Add proper timeout configuration and handling
- [ ] [Error Handling] Silent failures in `save_model_entry` — agent_cli/config.py:154-183 — Log errors and provide actionable feedback

## Low Priority
- [ ] [Time & Space Complexity] Redundant message serialization in `ContextManager` — agent_cli/context/manager.py:153-163 — Cache serialized messages when unchanged
- [ ] [Readability] Long function `_run_shell_inline` — agent_cli/main.py:28-46 — Extract error handling into separate function
- [ ] [Readability] Magic strings in color mapping — agent_cli/render.py:23-32 — Define constants for color names
- [ ] [Duplicate Code] Similar argument substitution logic in skills and delegation — agent_cli/skills/executor.py:13-27 and agent_cli/tools/delegate.py — Create shared utility function
- [ ] [Error Handling] Inconsistent error messages across modules — Multiple files — Standardize error message format and content
- [ ] [Readability] Long parameter lists in many functions — Multiple files — Consider using dataclasses or configuration objects
- [ ] [Time & Space Complexity] Inefficient list comprehensions in tool conversion — agent_cli/tools/registry.py:132-160 — Pre-compute when possible
- [ ] [Error Handling] Missing validation in `_validate_subtask` — agent_cli/tools/delegate.py:17-27 — Add more robust validation for self-contained tasks
- [ ] [Readability] Magic numbers in token estimation — agent_cli/context/token_estimator.py:12 — Define constants for token estimation parameters
