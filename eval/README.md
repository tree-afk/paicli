# PaiCLI SWE-bench Pro Evaluation

This harness runs PaiCLI on SWE-bench Pro V2 instances and grades the resulting
patch. It does not change the ReAct, memory, MCP, or multi-agent runtime.

## Layout

```text
eval/
  swebench_pro_runner.py   # per-instance agent run + clean-replay grading
  container_bash.py        # bash alias that executes inside the task container
  runner.py                # engine factory and prompt runner
  trace.py                 # event -> tool calls, results, usage, compression
```

## Run

```bash
uv run python -m eval.swebench_pro_runner \
  --manifest eval/results/swebench_pro/manifest.jsonl \
  --output eval/results/swebench_pro/run_name \
  --max-turns 30
```

Per instance, the runner:

1. pulls the task image and extracts the repo at `base_commit`;
2. copies a pristine `base_repo` for grading;
3. starts a `--network=none` container with only the repo mounted at `/app`;
4. runs PaiCLI on the host, with `bash`/`execute_command` overridden to
   `docker exec` into that container;
5. collects `agent_patch.diff` from the working tree;
6. grades in a fresh container: reset to `base_commit`, apply test patch, apply
   agent patch, run the selected test files.

`case.json` stores `gold_patch` and `test_patch` as `<redacted>`; the real test
patch is written separately and only used by the grader.

## Outputs

- `result.json` — status, resolved, turns, usage, cost, changed files, patch size,
  apply/test/docker return codes
- `agent_trace.json` — tool calls, tool results, final text, context compressions
- `verify.log` / `verify.sh` — grading replay and console output
- `agent_prompt.txt`, `agent_patch.diff`, `test_patch.patch`

## Status values

| status | meaning |
|---|---|
| `resolved` | grading replay passed |
| `not_resolved` | tests ran and failed |
| `no_patch` | agent produced an empty diff |
| `test_tampered` | agent modified test files |
| `patch_apply_failed` | agent patch did not apply |
| `collection_error` | pytest collected no tests, or was interrupted (exit 2/5); e.g. the patch broke an import |
| `infra_error` | test patch did not apply, or no junit report (grading environment problem) |

`resolved` means every `fail_to_pass` test now passes and no `pass_to_pass`
test regressed, counted per test id from the junit report (official SWE-bench
rule). A `pass_to_pass` test that is skipped is not a regression; a
`fail_to_pass` test that is skipped is not a pass. Some upstream manifests were
whitespace-split, so an entry such as `test_x[The` matches all collected
`test_x[The ...]` cases; `approximate_entries` counts those matches so they
stay visible instead of being silently credited.

## Safety

- The agent container mounts only the task repo and has no network.
- `web_search` / `web_fetch` are removed; `curl`, `wget`, `git fetch/clone`,
  `ssh`, `scp`, `pip install`, `apt`, `docker` are command-guarded.
- Each bash call is capped at 1200s, matching the per-run agent budget.
- Do not point the runner at a production workspace.
