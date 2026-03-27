---
name: summarize
description: Read and summarize a file or directory concisely
allowed-tools: [read_file, shell]
max-iter: 10
argument-hint: "<path>"
---

Read $ARGUMENTS and provide a concise summary.

If the path is a file:
- What the file does (one paragraph)
- Key functions/classes and their purposes
- Dependencies and relationships with other modules

If the path is a directory:
- Use `shell` to list files first
- Overall purpose of the directory
- Key files and their roles
- How the components connect

Keep the summary under 500 words. Be specific — mention function names, class names, and concrete behaviors rather than vague descriptions.
