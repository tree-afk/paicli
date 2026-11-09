---
name: defect-fix
description: |
  Use this skill to fix a reported defect or a failing test in a local repository. It guides reproduction, localization, minimal patching, and verification.
version: "1.0.0"
author: PaiCLI
tags: [debugging, bugfix, tests, repair]
---

# Defect Fix

Use this skill when the user reports a bug or a failing test.

## Workflow

1. Reproduce before changing code. Run the exact failing test or a minimal command and keep the output.
2. Find the root cause. Read the relevant code and tests, trace the failure, and state the cause in one sentence.
3. Make the smallest fix. Do not refactor unrelated code, rename public APIs, or reformat files.
4. Do not modify tests, assertions, fixtures, pytest configuration, or skip markers. If a test is wrong, explain why instead of changing it.
5. Verify. Run the originally failing test, then the surrounding test suite. Report the exact commands and results.
6. If the fix fails, return to step 2 with the new evidence. Do not guess.

## Output

Report: root cause, changed files, exact verification commands, test results, and any remaining risk.
