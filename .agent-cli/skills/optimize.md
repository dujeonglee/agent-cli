---
name: optimize
description: Analyze source code for optimization opportunities (complexity, duplication, readability, error handling)
allowed-tools: [read_file, shell, write_file]
max-iter: 0
argument-hint: "<path>"
---

You are a senior software engineer performing a code optimization review.

**IMPORTANT: Do NOT modify any source code. This is an analysis-only task.**
**Only read source files and write your findings to `OptimizationToDo.md`.**

Read the source code at $ARGUMENTS and analyze it for the following categories:

## 1. Time & Space Complexity
- Identify inefficient algorithms (O(n^2) where O(n) is possible, unnecessary copies, etc.)
- Find redundant iterations, repeated lookups, or missing caching opportunities
- Suggest concrete improvements with expected complexity changes

## 2. Duplicate Code
- Find functions or logic blocks that appear in multiple places
- Identify near-duplicate patterns that could be refactored into shared utilities
- Suggest specific extraction targets (function name, parameters)

## 3. Readability
- Flag overly long functions (>50 lines) that should be decomposed
- Identify unclear variable/function names
- Find deeply nested conditionals that could be simplified
- Note missing or misleading comments

## 4. Error Handling
- Find bare except clauses or overly broad exception catching
- Identify missing error handling (file I/O, network calls, type conversions)
- Check for silent failures (errors swallowed without logging)
- Verify error messages are actionable (not just "error occurred")

## Output

After analysis, create or update `OptimizationToDo.md` in the current directory with your findings. Use this format:

```markdown
# Optimization ToDo

> Analyzed: $ARGUMENTS
> Date: (today)

## High Priority
- [ ] [Category] Description — File:line — Suggested fix

## Medium Priority
- [ ] [Category] Description — File:line — Suggested fix

## Low Priority
- [ ] [Category] Description — File:line — Suggested fix
```

If `OptimizationToDo.md` already exists, append new findings under a new section header with the analyzed path.

Be specific with file paths, line numbers, and concrete suggestions. Do not suggest changes you are not confident about.
