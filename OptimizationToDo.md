# Optimization ToDo

> Analyzed: agent_cli/ (all Python files, 42 files total)
> Date: 2026-03-22

## High Priority

- [ ] [Duplicate Code] Tool execution logic is duplicated between native tool calling path and text parsing path in `run_loop` — `loop.py:128-192` (native) vs `loop.py:223-292` (text parsing). Both blocks perform the same delegate check, validate_tool_input, execute_tool, truncation, and error formatting. — Suggested fix: Extract a shared `_execute_single_tool(tool_name, tool_input, tools_list, include_delegate, ...)` function that returns an observation string, called from both paths.

- [ ] [Duplicate Code] `convert_to_anthropic_tools` and `convert_to_openai_tools` in `registry.py:111-187` share ~90% identical logic (iterate tool_names, look up schema, build dict, optionally add delegate). — Suggested fix: Extract a shared `_convert_tools(tool_names, include_delegate, formatter_fn)` where `formatter_fn` transforms a ToolSchema into provider-specific format.

- [ ] [Duplicate Code] `_format_tool_block` in `system_prompt.py:82-103` duplicates the logic of `get_tool_descriptions` in `registry.py:190-201` — both iterate TOOL_SCHEMAS and format descriptions with JSON params. — Suggested fix: Unify into a single function in `registry.py` that accepts an optional tool name filter and delegate flag.

- [ ] [Error Handling] Bare `except Exception: pass` silently swallows errors when loading `models.json` — `config.py:68-69`. A malformed JSON file would be silently ignored, making debugging very difficult. — Suggested fix: Log the exception to stderr (similar to `save_model_entry` on line 109) so users know when a config file is corrupt.

- [ ] [Error Handling] `_detect_ollama_capabilities` catches all exceptions and returns `None` — `compat.py:184-185`. Network errors, JSON decode errors, and permission errors are all silently discarded. — Suggested fix: Log the error to stderr and return None, or at minimum distinguish between "model not found" vs "Ollama not running".

- [ ] [Error Handling] `_detect_openai_compat_capabilities` has the same issue — `compat.py:281-282`. All exceptions silently return None. — Suggested fix: Same as above, add stderr logging.

- [ ] [Time & Space] `_try_json_parse` uses greedy `re.search(r"\{[\s\S]*\}", stripped)` which matches from the first `{` to the last `}` — `react_parser.py:117`. For LLM output containing multiple JSON-like blocks, this will incorrectly match across boundaries. More critically, `[\s\S]*` with backtracking can be slow on large inputs. — Suggested fix: Use the balanced-brace extraction from `json_repair._extract_json_block` which already handles this correctly with O(n) character scanning.

- [ ] [Readability] `run_loop` function is 290 lines long (`loop.py:28-317`) — extremely difficult to reason about. — Suggested fix: Extract the native tool calling path (lines 128-192) into `_handle_native_tool_calls(...)` and the text parsing path (lines 194-313) into `_handle_text_parsed_response(...)`.

## Medium Priority

- [ ] [Readability] `chat` command handler is 130+ lines (`main.py:440-641`) with deeply nested conditionals for `/plan`, `/skills`, `/sh`, and skill dispatch. — Suggested fix: Extract each command handler into a separate function (e.g., `_handle_chat_plan`, `_handle_chat_skill_list`).

- [ ] [Duplicate Code] Markdown fence stripping regex `re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I)` appears in three places: `react_parser.py:100-101`, `json_repair.py:42-43`, and `plan_parser.py:58-59`. — Suggested fix: Extract to a shared utility function `strip_markdown_fences(text)` in the parsing package, reuse the one already defined in `react_parser.py`.

- [ ] [Time & Space] `_total_chars` in `context/manager.py:77-79` recomputes `sum(len(m["content"]) for m in self.messages)` on every `add()` call. With many messages, this is O(n) per add. — Suggested fix: Maintain a running `_char_count` integer, increment on `add()`, decrement when compressing. Reduces `add()` from O(n) to O(1).

- [ ] [Time & Space] `_fix_missing_brackets` in `json_repair.py:144-174` appends characters via string concatenation in a while loop (`text += stack.pop()`). For deeply nested JSON, this creates O(k) string copies. — Suggested fix: Use `text + "".join(reversed(stack))` for a single concatenation.

- [ ] [Error Handling] `_compress` in `context/manager.py:115-120` catches all exceptions but has a redundant `pass` after the `print` — `manager.py:120`. More importantly, if compression repeatedly fails, `add()` will attempt compression on every subsequent call since `_total_chars()` still exceeds threshold. — Suggested fix: Remove redundant `pass`. Consider adding a backoff mechanism (e.g., a `_compression_failed` flag that temporarily doubles `max_context_chars`).

- [ ] [Error Handling] `tool_shell` does not sanitize or limit the command string — `shell.py:10`. An empty command string would execute an empty shell command. — Suggested fix: Validate that `cmd` is non-empty before executing, raise RuntimeError with an actionable message.

- [ ] [Readability] `_dispatch_skill` has 12 parameters (`main.py:67-107`) — too many positional-style parameters. — Suggested fix: Group related parameters into a config dataclass (e.g., `LoopConfig(max_iter, verbose, quiet, max_depth, delegate_timeout)`).

- [ ] [Duplicate Code] `ContextManager` creation is duplicated in `chat` — once at line 491-495 and again at line 530-534 (for `/clear`). — Suggested fix: Extract to a helper `_create_context_manager(provider, model, capabilities)`.

- [ ] [Error Handling] `was_runtime_detected()` is decorated with `@lru_cache(maxsize=1024)` on a function that takes no arguments — `compat.py:50-53`. This means the cache will always return the first result and never update when `_last_was_runtime_detected` changes. The `lru_cache` is effectively a bug here. — Suggested fix: Remove `@lru_cache` decorator entirely; the function just reads a module-level variable and needs no caching.

- [ ] [Time & Space] `_SEARCH_PATHS` in `config.py:25-29` evaluates `Path.cwd()` at module import time. If the working directory changes after import, the cached path will be stale. — Suggested fix: Compute `Path.cwd()` lazily inside `_find_models_json()` and `_load_registry()`, or document this as intentional.

## Low Priority

- [ ] [Readability] `edit_plan` in `reviewer.py:47-108` is 60+ lines with nested if/elif/else for command parsing. — Suggested fix: Use a command dispatch dict mapping prefixes to handler functions.

- [ ] [Readability] Variable name `r` is used for HTTP responses across all provider files (`anthropic.py:57`, `openai_compat.py:64`, `ollama.py:68`). Single-letter variables reduce readability. — Suggested fix: Rename to `response` or `http_response`.

- [ ] [Readability] `_NIBBLE` and `_DICT` variable names in `read_file.py:13-14` are cryptic with no explanatory comment. — Suggested fix: Add a brief comment explaining this is a lookup table for CRC32-to-2-char encoding.

- [ ] [Error Handling] `fuzzy_verify_ref` in `edit_file.py:56-59` accepts any non-empty line as a valid fuzzy match when normalized hash also fails. This is overly lenient and could silently edit the wrong line. — Suggested fix: Add a warning in the return value or require at least some content similarity before accepting.

- [ ] [Time & Space] `substitute_arguments` in `skills/executor.py:13-23` uses `re.sub(r"\$\d+", "", result)` to clean unused placeholders, but also calls `result.replace(f"${i}", arg)` in a loop. If template has many placeholders, this is O(n*k) string operations. — Suggested fix: Use `re.sub` with a replacement function for a single-pass substitution.

- [ ] [Readability] `_TOOL_KEYWORDS` regex patterns in `planning/executor.py:15-24` mix Korean characters with English. The Korean keywords (읽, 생성, 작성, 수정, 변경, 실행, 테스트) are not commented. — Suggested fix: Add a comment explaining multilingual support.

- [ ] [Time & Space] `_serialize_messages` in `context/manager.py:122-135` truncates content at 2000 chars per message, then joins with `"\n\n".join(parts)`. The truncation threshold is hardcoded. — Suggested fix: Make the truncation threshold configurable or derive it from the context window size.

- [ ] [Error Handling] `Plan.load` in `planning/models.py:65-68` does not validate the loaded JSON structure. A file with valid JSON but wrong schema would produce cryptic KeyError. — Suggested fix: Add schema validation or wrap in try/except with an actionable error message.

- [ ] [Readability] `_lock = None  # Will be set to threading.Lock if needed` in `compat.py:43` — unused dead code. The lock is never set or used anywhere. — Suggested fix: Remove the unused variable.

- [ ] [Duplicate Code] `yes, no = "✓", "✗"` is defined twice in `render.py` — at line 131 and again at line 167. — Suggested fix: Define `YES_MARK` and `NO_MARK` as module-level constants.
