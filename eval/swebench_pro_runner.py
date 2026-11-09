from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path
from typing import Any

from eval.container_bash import create_container_tools
from eval.runner import build_engine, run_prompt
from eval.trace import CaseTrace

ROOT = Path(__file__).resolve().parents[1]
WORKDIR = "/app"
DOCKER_MIRROR_PREFIX = "ghcr.nju.edu.cn/"
SWEBENCH_EXCLUDED_TOOLS = {"web_search", "web_fetch"}
SWEBENCH_BLOCKED_COMMANDS = [
    "curl",
    "wget",
    "git fetch",
    "git clone",
    "git remote",
    "ssh",
    "scp",
    "sftp",
    "pip install",
    "uv pip",
    "apt-get",
    "apt ",
    "yum",
    "dnf",
    "docker",
    "podman",
    "http://",
    "https://",
]


def _is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return False
    name = parts[-1]
    if any(part in {"test", "tests"} for part in parts[:-1]):
        return True
    if name in {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg"}:
        return True
    return name.startswith("test_") or name.endswith("_test.py")


def _build_prompt(row: dict[str, Any]) -> str:
    return (
        "You are working in a local repository. Fix the issue below.\n\n"
        "IMPORTANT: bash runs inside a Docker container where the repository is "
        "mounted at /app. The container has no network access, so upstream code "
        "cannot be fetched. bash and the file tools (read_file, grep, glob, "
        "edit_file, write_file) see the same repository; use /app/... or "
        "repository-relative paths. The host working-directory path shown in the "
        "system prompt does not exist inside the container.\n\n"
        f"## Issue\n{row['problem_statement']}\n\n"
        f"## Requirements\n{row['requirements']}\n\n"
        f"## Interfaces\n{row['interface']}\n\n"
        "Work only in the repository. Do not modify tests or test configuration. "
        "Make the smallest source change that satisfies the requirements. "
        "You may use read_file, grep, glob, edit_file, write_file and bash. "
        "Run tests inside the task container with bash, using paths relative to /app "
        "(for example, python -m pytest test/...).\n"
    )


def _parse_rc(output: str, key: str) -> int | None:
    match = re.search(rf"{re.escape(key)}=(\d+)", output)
    return int(match.group(1)) if match else None


def _docker_env() -> dict[str, str]:
    env = dict(os.environ)
    if "XDG_RUNTIME_DIR" not in env and hasattr(os, "getuid"):
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    env.setdefault("DOCKER_HOST", f"unix://{env['XDG_RUNTIME_DIR']}/docker.sock")
    return env


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 300,
    check: bool = False,
) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=_docker_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(args)}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed


def _ensure_image(image: str) -> None:
    exists = _run(["docker", "image", "inspect", image], timeout=60)
    if exists.returncode == 0:
        return
    mirror = image.replace("ghcr.io/", DOCKER_MIRROR_PREFIX, 1)
    _run(["docker", "pull", mirror], timeout=1800, check=True)
    _run(["docker", "tag", mirror, image], timeout=60, check=True)


def _start_bash_container(image: str, repo: Path, name: str) -> str:
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-v",
            f"{repo}:/app",
            "-w",
            "/app",
            "--network=none",
            "--memory=8g",
            "--pids-limit=2048",
            image,
            "sleep",
            "infinity",
        ],
        timeout=300,
        check=True,
    )
    state = _run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        timeout=60,
        check=True,
    )
    if state.stdout.strip().lower() != "true":
        raise RuntimeError(f"bash container is not running: {name}")
    return name


def _stop_bash_container(name: str) -> None:
    with contextlib.suppress(Exception):
        _run(["docker", "rm", "-f", name], timeout=120, check=False)


def _extract_repo(image: str, destination: Path) -> None:
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    completed = _run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{destination}:/out",
            "--network=none",
            "--memory=8g",
            "--pids-limit=2048",
            image,
            "bash",
            "-lc",
            (
                "if [ -d /app ]; then cp -a /app/. /out/; "
                "elif [ -d /testbed ]; then cp -a /testbed/. /out/; "
                "else echo NO_REPO; exit 2; fi"
            ),
        ],
        timeout=300,
        check=True,
    )
    if not (destination / ".git").exists():
        raise RuntimeError(f"repository was not extracted: {completed.stdout}")


def _clean_artifacts(repo: Path) -> None:
    for path in repo.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)
    for name in (".pytest_cache", ".mypy_cache", ".ruff_cache"):
        for path in repo.rglob(name):
            shutil.rmtree(path, ignore_errors=True)
    for path in repo.rglob("*.pyc"):
        path.unlink(missing_ok=True)


def _prepare_base(repo: Path, base_commit: str, fetch_remote: str | None) -> None:
    exists = _run(["git", "cat-file", "-t", base_commit], cwd=repo, timeout=60)
    if exists.returncode != 0:
        if not fetch_remote:
            raise RuntimeError(f"base commit is missing and no fetch remote: {base_commit}")
        _run(["git", "remote", "remove", "eval-remote"], cwd=repo, timeout=60)
        _run(["git", "remote", "add", "eval-remote", fetch_remote], cwd=repo, check=True)
        _run(
            ["git", "fetch", "--depth", "1", "eval-remote", base_commit],
            cwd=repo,
            timeout=900,
            check=True,
        )
    _run(["git", "reset", "--hard", base_commit], cwd=repo, timeout=120, check=True)
    _run(["git", "clean", "-fd"], cwd=repo, timeout=120, check=True)


def _collect_patch(repo: Path, base_commit: str) -> tuple[str, list[str]]:
    _clean_artifacts(repo)
    _run(["git", "add", "-A"], cwd=repo, timeout=120, check=True)
    patch = _run(["git", "diff", "--cached", base_commit], cwd=repo, timeout=120, check=True).stdout
    changed = [
        line
        for line in _run(
            ["git", "diff", "--cached", "--name-only", base_commit],
            cwd=repo,
            timeout=120,
            check=True,
        ).stdout.splitlines()
        if line.strip()
    ]
    return patch, changed


def _redacted_case(row: dict[str, Any]) -> dict[str, Any]:
    case = dict(row)
    for field in ("gold_patch", "test_patch"):
        if field in case:
            case[field] = "<redacted>"
    return case


def _run_agent(
    prompt: str,
    repo: Path,
    *,
    timeout: float,
    max_turns: int,
    tool_overrides: dict[str, Any] | None = None,
) -> CaseTrace:
    try:
        engine = build_engine(
            repo,
            compression=True,
            max_turns=max_turns,
            excluded_tools=SWEBENCH_EXCLUDED_TOOLS,
            command_blacklist_extra=SWEBENCH_BLOCKED_COMMANDS,
            tool_overrides=tool_overrides,
        )
    except Exception as exc:  # noqa: BLE001 - record setup failures
        return CaseTrace(error=f"engine setup failed: {exc}")
    try:
        return asyncio.run(asyncio.wait_for(run_prompt(prompt, engine), timeout=timeout))
    except TimeoutError:
        return CaseTrace(error=f"agent timeout after {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001 - record agent failures
        return CaseTrace(error=f"agent failed: {exc}")


def _junit_node_id(file_name: str, classname: str, name: str) -> str:
    if not name:
        return ""
    if not file_name:
        # pytest 8 junit omits file=; classname still carries the module path.
        return f"{classname}::{name}" if classname else ""
    module = file_name[:-3] if file_name.endswith(".py") else file_name
    module = module.replace("/", ".")
    if classname.startswith(module + "."):
        return f"{file_name}::{classname[len(module) + 1 :]}::{name}"
    return f"{file_name}::{name}"


def _canonical(node: str) -> tuple[str, ...]:
    parts = node.split("::")
    head = parts[0]
    if head.endswith(".py"):
        head = head[:-3]
    return tuple(part for part in head.replace("/", ".").split(".") if part) + tuple(parts[1:])


def _is_fragment(node: str) -> bool:
    """True when whitespace splitting cut a parametrized id mid-bracket."""
    last = node.split("::")[-1]
    return last.count("[") > last.count("]")


def _parse_junit(path: Path) -> dict[str, str]:
    try:
        data = path.read_bytes()
    except OSError:
        return {}
    # junit.xml is written by code under test; reject DTD/entity declarations.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return {}
    try:
        tree = ET.fromstring(data)
    except ET.ParseError:
        return {}
    outcomes: dict[str, str] = {}
    for case in tree.iter("testcase"):
        node = _junit_node_id(case.get("file", ""), case.get("classname", ""), case.get("name", ""))
        if not node:
            continue
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        outcomes[node] = outcome
    return outcomes


def _base_node_id(node: str) -> str:
    head, sep, tail = node.rpartition("::")
    return head + sep + tail.split("[", 1)[0]


def _canonical_prefix_matches(expected: tuple[str, ...], actual: tuple[str, ...]) -> bool:
    if len(actual) < len(expected):
        return False
    if expected[:-1] != actual[: len(expected) - 1]:
        return False
    return bool(expected) and actual[len(expected) - 1].startswith(expected[-1])


def _count_expected(
    expected: list[str],
    outcomes: dict[str, str],
    actual: dict[str, tuple[str, ...]],
    base_index: dict[str, list[str]],
    *,
    skipped_is_pass: bool,
) -> dict[str, Any]:
    seen: set[str] = set()
    failed: list[str] = []
    missing: list[str] = []
    approximate = 0
    for node in expected:
        canonical = _canonical(node)
        matches = [n for n, c in actual.items() if c == canonical]
        approximate_match = False
        if not matches and _is_fragment(node):
            # Upstream manifests were whitespace-split, so a parametrised id can
            # arrive as "..::test_x[The" instead of "..::test_x[The first line]".
            matches = [n for n, c in actual.items() if _canonical_prefix_matches(canonical, c)]
            approximate_match = bool(matches)
        if not matches:
            matches = base_index.get(_base_node_id(node)) or []
            approximate_match = bool(matches)
        if not matches:
            missing.append(node)
            continue
        approximate += int(approximate_match)
        for actual_node in matches:
            if actual_node in seen:
                continue
            seen.add(actual_node)
            outcome = outcomes[actual_node]
            if outcome != "passed" and not (skipped_is_pass and outcome == "skipped"):
                failed.append(actual_node)
    return {"seen": seen, "failed": failed, "missing": missing, "approximate": approximate}


def _summarize_tests(
    outcomes: dict[str, str],
    fail_to_pass: list[str],
    pass_to_pass: list[str],
) -> dict[str, Any]:
    actual = {node: _canonical(node) for node in outcomes}
    base_index: dict[str, list[str]] = {}
    for node in actual:
        base_index.setdefault(_base_node_id(node), []).append(node)

    f2p = _count_expected(fail_to_pass, outcomes, actual, base_index, skipped_is_pass=False)
    p2p = _count_expected(pass_to_pass, outcomes, actual, base_index, skipped_is_pass=True)
    missing = f2p["missing"] + p2p["missing"]
    return {
        "f2p_passed": len(f2p["seen"]) - len(f2p["failed"]),
        "f2p_total": len(f2p["seen"]) + len(f2p["missing"]),
        "f2p_failed": f2p["failed"] + f2p["missing"],
        "p2p_passed": len(p2p["seen"]) - len(p2p["failed"]),
        "p2p_total": len(p2p["seen"]) + len(p2p["missing"]),
        "p2p_failed": p2p["failed"] + p2p["missing"],
        "missing_tests": missing,
        "approximate_entries": f2p["approximate"] + p2p["approximate"],
    }


NODE_RUNNER = (
    "import sys\n"
    "\n"
    "import pytest\n"
    "\n"
    'ids = [line.strip() for line in open("/task/nodeids.txt", encoding="utf-8") if line.strip()]\n'
    'sys.exit(pytest.main(["-q", "--junitxml=/task/junit.xml"] + ids))\n'
)


def _verify(
    row: dict[str, Any],
    image: str,
    base_repo: Path,
    task_dir: Path,
    patch: str,
    test_files: list[str],
) -> tuple[str, dict[str, Any]]:
    (task_dir / "test_patch.patch").write_text(row["test_patch"], encoding="utf-8")
    (task_dir / "agent_patch.diff").write_text(patch, encoding="utf-8")
    summary = _summarize_tests(
        {},
        json.loads(row["fail_to_pass"]),
        json.loads(row["pass_to_pass"]),
    )
    if not patch.strip():
        # `git apply` on an empty diff exits 1, which would otherwise read as
        # "the agent's patch did not apply" instead of "the agent wrote nothing".
        return "no_patch", {
            "test_patch_apply_rc": None,
            "agent_patch_apply_rc": None,
            "test_rc": None,
            "docker_rc": None,
            "collected_tests": 0,
            **summary,
        }
    prelude = (
        "#!/bin/bash\n"
        "set -u\n"
        f"cd {WORKDIR}\n"
        f"git reset --hard {row['base_commit']}\n"
        "echo reset_rc=$?\n"
        "git clean -fd\n"
        f"git checkout {row['base_commit']}\n"
        "mkdir -p /tests\n"
        "cp /task/test_patch.patch /tests/test_patch.patch\n"
        "git apply /tests/test_patch.patch\n"
        "echo test_patch_apply_rc=$?\n"
        "git apply /task/agent_patch.diff\n"
        "echo agent_patch_apply_rc=$?\n"
    )
    script = prelude + (
        "rm -f /task/junit.xml\n"
        f"python -m pytest -q --junitxml=/task/junit.xml {' '.join(test_files)}\n"
        "echo test_rc=$?\n"
    )
    (task_dir / "verify.sh").write_text(script, encoding="utf-8")
    completed = _run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{task_dir}:/task",
            "-v",
            f"{base_repo}:{WORKDIR}",
            "--network=none",
            "--memory=8g",
            "--pids-limit=2048",
            image,
            "bash",
            "/task/verify.sh",
        ],
        timeout=900,
    )
    output = completed.stdout + completed.stderr
    (task_dir / "verify.log").write_text(output, encoding="utf-8")
    reset_rc = _parse_rc(output, "reset_rc")
    test_patch_rc = _parse_rc(output, "test_patch_apply_rc")
    agent_apply_rc = _parse_rc(output, "agent_patch_apply_rc")
    test_rc = _parse_rc(output, "test_rc")
    junit_path = task_dir / "junit.xml"
    outcomes = _parse_junit(junit_path)
    fallback: str | None = None
    if not outcomes and reset_rc == 0 and test_patch_rc == 0 and agent_apply_rc == 0:
        # Some suites (qutebrowser) segfault on a test that is not graded; pytest
        # dies before writing junit and every graded test loses its outcome.
        # Re-running just the graded node ids keeps the crash out of the session.
        node_ids = [*json.loads(row["fail_to_pass"]), *json.loads(row["pass_to_pass"])]
        if node_ids:
            fallback = "node_ids"
            (task_dir / "nodeids.txt").write_text("\n".join(node_ids) + "\n", encoding="utf-8")
            (task_dir / "node_runner.py").write_text(NODE_RUNNER, encoding="utf-8")
            retry = prelude + (
                "rm -f /task/junit.xml\npython /task/node_runner.py\necho test_rc=$?\n"
            )
            (task_dir / "verify_fallback.sh").write_text(retry, encoding="utf-8")
            completed = _run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{task_dir}:/task",
                    "-v",
                    f"{base_repo}:{WORKDIR}",
                    "--network=none",
                    "--memory=8g",
                    "--pids-limit=2048",
                    image,
                    "bash",
                    "/task/verify_fallback.sh",
                ],
                timeout=900,
            )
            output = completed.stdout + completed.stderr
            (task_dir / "verify_fallback.log").write_text(output, encoding="utf-8")
            reset_rc = _parse_rc(output, "reset_rc")
            test_patch_rc = _parse_rc(output, "test_patch_apply_rc")
            agent_apply_rc = _parse_rc(output, "agent_patch_apply_rc")
            test_rc = _parse_rc(output, "test_rc")
            outcomes = _parse_junit(junit_path)
    summary = _summarize_tests(
        outcomes,
        json.loads(row["fail_to_pass"]),
        json.loads(row["pass_to_pass"]),
    )
    details = {
        "reset_rc": reset_rc,
        "test_patch_apply_rc": test_patch_rc,
        "agent_patch_apply_rc": agent_apply_rc,
        "test_rc": test_rc,
        "docker_rc": completed.returncode,
        "collected_tests": len(outcomes),
        "test_fallback": fallback,
        **summary,
    }
    if reset_rc != 0:
        # The tree is not at base_commit, so any patch or test result is bogus.
        return "infra_error", details
    if test_patch_rc != 0:
        return "infra_error", details
    if agent_apply_rc != 0:
        return "patch_apply_failed", details
    if not junit_path.exists():
        return "infra_error", details
    expected_tests = summary["f2p_total"] + summary["p2p_total"]
    # pytest 7.4 exits 1 when every selected module fails to import (e.g. the
    # patch broke an import), so rc alone cannot tell that from failing tests.
    if test_rc in {2, 5} or (not outcomes and expected_tests):
        return "collection_error", details
    if (
        summary["f2p_passed"] == summary["f2p_total"]
        and summary["p2p_passed"] == summary["p2p_total"]
    ):
        return "resolved", details
    return "not_resolved", details


def run_instance(
    row: dict[str, Any],
    output_root: Path,
    *,
    image_override: str | None = None,
    fetch_remote: str | None = None,
    max_turns: int = 30,
) -> dict[str, Any]:
    instance_id = row["instance_id"]
    image = image_override or row["docker_image"]
    task_dir = output_root / instance_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "case.json").write_text(
        json.dumps(_redacted_case(row), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _ensure_image(image)
    repo = task_dir / "repo"
    _extract_repo(image, repo)
    _prepare_base(repo, row["base_commit"], fetch_remote)
    base_repo = task_dir / "base_repo"
    shutil.rmtree(base_repo, ignore_errors=True)
    shutil.copytree(repo, base_repo, symlinks=True)
    prompt = _build_prompt(row)
    (task_dir / "agent_prompt.txt").write_text(prompt, encoding="utf-8")
    container_name = f"paicli-bash-{uuid.uuid4().hex}"
    _start_bash_container(image, repo, container_name)
    started = time.perf_counter()
    try:
        trace = _run_agent(
            prompt,
            repo,
            timeout=1200,
            max_turns=max_turns,
            tool_overrides=create_container_tools(container_name),
        )
    finally:
        agent_duration = round(time.perf_counter() - started, 2)
        _stop_bash_container(container_name)
    patch, changed_files = _collect_patch(repo, row["base_commit"])
    (task_dir / "agent_patch.diff").write_text(patch, encoding="utf-8")
    (task_dir / "agent_trace.json").write_text(
        json.dumps(asdict(trace), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    test_files_modified = any(_is_test_path(path) for path in changed_files)
    test_files = json.loads(row["selected_test_files_to_run"])
    result: dict[str, Any] = {
        "instance_id": instance_id,
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "image_used": image,
        "max_turns": max_turns,
        "agent_duration_s": agent_duration,
        "turns": trace.turns,
        "total_tokens": int(trace.usage.get("total_tokens") or 0),
        "usage": trace.usage,
        "cost": trace.cost,
        "tool_calls": [call.get("name") for call in trace.tool_calls],
        "agent_error": trace.error,
        "changed_files": changed_files,
        "test_files_modified": test_files_modified,
        "patch_lines": len(patch.splitlines()),
        "test_files": test_files,
        "fail_to_pass_count": len(json.loads(row["fail_to_pass"])),
        "pass_to_pass_count": len(json.loads(row["pass_to_pass"])),
    }
    if not patch.strip():
        # A provider/transport failure leaves no patch, but that is not the
        # model producing nothing; keep it out of the model's denominator.
        result["status"] = "infra_error" if trace.error else "no_patch"
        result["resolved"] = False
    elif test_files_modified:
        result["status"] = "test_tampered"
        result["resolved"] = False
    else:
        status, details = _verify(row, image, base_repo, task_dir, patch, test_files)
        result.update(details)
        result["status"] = status
        result["resolved"] = status == "resolved"
    (task_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def _load_env_file(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    _load_env_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Run a SWE-bench Pro manifest.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", default=str(ROOT / "eval" / "results" / "swebench_pro"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--instance", action="append", default=[])
    parser.add_argument("--image", help="Use one image for every task")
    parser.add_argument("--fetch-remote", help="Git remote used when a base commit is missing")
    parser.add_argument("--max-turns", type=int, default=30)
    args = parser.parse_args(argv)
    rows = [
        json.loads(line)
        for line in Path(args.manifest).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.instance:
        selected = set(args.instance)
        rows = [row for row in rows if row["instance_id"] in selected]
    if args.limit:
        rows = rows[: args.limit]
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    results = []
    for index, row in enumerate(rows, start=1):
        print(f"[{index}/{len(rows)}] {row['instance_id']}", flush=True)
        try:
            result = run_instance(
                row,
                output_root,
                image_override=args.image,
                fetch_remote=args.fetch_remote,
                max_turns=args.max_turns,
            )
        except Exception as exc:  # noqa: BLE001 - one task must not stop the suite
            result = {
                "instance_id": row["instance_id"],
                "status": "infra_error",
                "resolved": False,
                "agent_error": str(exc),
            }
        results.append(result)
        with (output_root / "results.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(
            f"  -> {result['status']} turns={result.get('turns', 0)} "
            f"tokens={result.get('total_tokens', 0)}",
            flush=True,
        )
    resolved = sum(1 for result in results if result.get("resolved"))
    print(f"resolved {resolved}/{len(results)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
