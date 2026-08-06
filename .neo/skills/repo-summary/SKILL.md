---
name: repo-summary
description: Inspect a repository and produce a concise, evidence-based overview of its purpose, architecture, commands, and tests. Use when a user asks to explain, summarize, onboard to, or map an unfamiliar codebase without changing files.
---

# Repository Summary

1. Inspect the repository without modifying files.
2. Use `glob`, `read_file`, and `grep` to examine the README, package manifest,
   entry points, core modules, and tests. Do not rely only on filenames.
   Search both `tests/*.py` and `**/test_*.py` before concluding that tests are
   absent, and read at least one representative test when tests are present.
3. Summarize the repository's purpose, main components, user-facing commands,
   configuration, and verification workflow.
4. Separate facts observed in files from any inference. State important unknowns
   instead of guessing.
5. Keep the answer concise unless the user asks for a deeper architecture map.
