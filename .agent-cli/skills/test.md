---
name: test
description: Generate unit tests for a source file
allowed-tools: [read_file, write_file, shell]
max-iter: 20
argument-hint: "<file_path>"
---

Read $ARGUMENTS and generate comprehensive unit tests for it.

## Guidelines

1. **Read the source file first** to understand all functions, classes, and edge cases
2. **Use pytest** as the test framework
3. **Test file naming**: if source is `agent_cli/foo.py`, write tests to `tests/test_foo.py`
4. **If tests already exist**, read them first and only add missing coverage
5. **Mock external dependencies** (file I/O, network calls, subprocess) — do not make real calls

## Coverage Targets

For each public function/method:
- Happy path (normal input → expected output)
- Edge cases (empty input, None, boundary values)
- Error cases (invalid input → appropriate exception or error handling)

## Quality

- Each test should test ONE thing
- Use descriptive test names: `test_<function>_<scenario>`
- Use fixtures to avoid repetition
- Assert specific values, not just "is not None"

## After Writing

Run `shell` command `pytest <test_file> -v` to verify all tests pass.
If any test fails, fix it before finishing.
