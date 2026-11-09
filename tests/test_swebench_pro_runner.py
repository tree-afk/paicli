from __future__ import annotations

import json
import re
import subprocess

import pytest

from eval import swebench_pro_runner as runner
from eval.swebench_pro_runner import (
    _build_prompt,
    _is_test_path,
    _parse_junit,
    _parse_rc,
    _summarize_tests,
)
from eval.trace import CaseTrace


def test_is_test_path_detects_tests_and_ignores_source():
    assert _is_test_path("test/units/utils/test_vars.py")
    assert _is_test_path("tests/test_app.py")
    assert _is_test_path("conftest.py")
    assert _is_test_path("pytest.ini")
    assert not _is_test_path("lib/ansible/vars/manager.py")
    assert not _is_test_path("src/calculator.py")


def test_build_prompt_contains_issue_and_constraints():
    prompt = _build_prompt(
        {
            "problem_statement": "TypeError in combine_vars",
            "requirements": "dict | VarsWithSources must work",
            "interface": "No new interfaces",
        }
    )

    assert "TypeError in combine_vars" in prompt
    assert "dict | VarsWithSources must work" in prompt
    assert "No new interfaces" in prompt
    assert "Do not modify tests" in prompt
    assert "task container" in prompt
    assert "/app" in prompt
    assert "host working-directory path" in prompt
    assert "does not exist inside the container" in prompt
    assert "no network access" in prompt
    assert "/app/... or repository-relative paths" in prompt


def test_parse_rc_reads_last_matching_value():
    output = "test_patch_apply_rc=0\nagent_patch_apply_rc=1\ntest_rc=2\n"

    assert _parse_rc(output, "test_patch_apply_rc") == 0
    assert _parse_rc(output, "agent_patch_apply_rc") == 1
    assert _parse_rc(output, "test_rc") == 2
    assert _parse_rc(output, "missing") is None


JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4">
    <testcase classname="tests.test_a.TestAlpha" name="test_ok" file="tests/test_a.py" line="1"/>
    <testcase classname="tests.test_a.TestAlpha" name="test_bad" file="tests/test_a.py" line="5">
      <failure message="assert 1 == 2">trace</failure>
    </testcase>
    <testcase classname="tests.test_b" name="test_free" file="tests/test_b.py" line="1">
      <error message="boom">trace</error>
    </testcase>
    <testcase classname="tests.test_b" name="test_skip" file="tests/test_b.py" line="9">
      <skipped message="no reason"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def _write_junit(path):
    path.write_text(JUNIT_XML, encoding="utf-8")


def test_parse_junit_maps_node_ids_and_outcomes(tmp_path):
    path = tmp_path / "junit.xml"
    _write_junit(path)

    outcomes = _parse_junit(path)

    assert outcomes["tests/test_a.py::TestAlpha::test_ok"] == "passed"
    assert outcomes["tests/test_a.py::TestAlpha::test_bad"] == "failed"
    assert outcomes["tests/test_b.py::test_free"] == "failed"
    assert outcomes["tests/test_b.py::test_skip"] == "skipped"


def test_summarize_tests_counts_passed_and_failed():
    outcomes = {
        "tests/test_a.py::TestAlpha::test_ok": "passed",
        "tests/test_a.py::TestAlpha::test_bad": "failed",
    }

    summary = _summarize_tests(
        outcomes,
        fail_to_pass=["tests/test_a.py::TestAlpha::test_ok"],
        pass_to_pass=["tests/test_a.py::TestAlpha::test_bad"],
    )

    assert summary["f2p_passed"] == 1
    assert summary["f2p_total"] == 1
    assert summary["p2p_passed"] == 0
    assert summary["p2p_total"] == 1
    assert summary["p2p_failed"] == ["tests/test_a.py::TestAlpha::test_bad"]


def test_summarize_tests_p2p_skip_is_not_a_regression():
    outcomes = {
        "tests/test_a.py::TestAlpha::test_skip": "skipped",
        "tests/test_a.py::TestAlpha::test_f2p_skip": "skipped",
    }

    summary = _summarize_tests(
        outcomes,
        fail_to_pass=["tests/test_a.py::TestAlpha::test_f2p_skip"],
        pass_to_pass=["tests/test_a.py::TestAlpha::test_skip"],
    )

    assert summary["f2p_passed"] == 0
    assert summary["p2p_passed"] == 1


def test_summarize_tests_matches_dotted_classname_without_file_attr():
    outcomes = {"tests.test_a.TestAlpha::test_ok": "passed"}

    summary = _summarize_tests(
        outcomes,
        fail_to_pass=["tests/test_a.py::TestAlpha::test_ok"],
        pass_to_pass=[],
    )

    assert summary["f2p_passed"] == 1
    assert summary["missing_tests"] == []


def test_summarize_tests_matches_space_split_param_fragments():
    outcomes = {
        "test.units.cli.test_doc::test_ttyify[The first line]": "passed",
        "test.units.cli.test_doc::test_ttyify[yolo-op]": "passed",
    }

    summary = _summarize_tests(
        outcomes,
        fail_to_pass=[],
        pass_to_pass=[
            "test/units/cli/test_doc.py::test_ttyify[The",
            "test/units/cli/test_doc.py::test_ttyify[yolo",
        ],
    )

    assert summary["p2p_passed"] == 2
    assert summary["p2p_total"] == 2
    assert summary["p2p_failed"] == []
    assert summary["approximate_entries"] == 2


def test_summarize_tests_does_not_prefix_match_complete_names():
    outcomes = {"tests/test_a.py::test_xy": "passed"}

    summary = _summarize_tests(
        outcomes,
        fail_to_pass=["tests/test_a.py::test_x"],
        pass_to_pass=[],
    )

    assert summary["f2p_passed"] == 0
    assert summary["missing_tests"] == ["tests/test_a.py::test_x"]


def test_summarize_tests_reports_missing_expected_tests():
    summary = _summarize_tests(
        {},
        fail_to_pass=["tests/test_a.py::TestAlpha::test_ok"],
        pass_to_pass=["tests/test_a.py::TestAlpha::test_gone"],
    )

    assert summary["f2p_passed"] == 0
    assert summary["p2p_passed"] == 0
    assert set(summary["missing_tests"]) == {
        "tests/test_a.py::TestAlpha::test_ok",
        "tests/test_a.py::TestAlpha::test_gone",
    }


def _row() -> dict:
    return {
        "instance_id": "instance_demo__repo-abc-v123",
        "repo": "demo/repo",
        "base_commit": "abc123",
        "docker_image": "ghcr.io/demo/image:tag",
        "problem_statement": "fix the defect",
        "requirements": "no new interfaces",
        "interface": "none",
        "selected_test_files_to_run": json.dumps(["tests/test_a.py"]),
        "fail_to_pass": json.dumps(["tests/test_a.py::test_x"]),
        "pass_to_pass": json.dumps([]),
        "gold_patch": "GOLD_SECRET_123",
        "test_patch": "TEST_SECRET_456",
    }


def _install_run_instance_mocks(monkeypatch, events, overrides, *, agent_error=None):
    def fake_extract(_image, destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / ".git").mkdir()
        (destination / "lib").mkdir()
        (destination / "lib" / "a.py").write_text("value = 1\n", encoding="utf-8")

    def fake_start(image, repo, name):
        events.append(("start", name, image, repo))
        return name

    def fake_stop(name):
        events.append(("stop", name))

    def fake_agent(prompt, repo, *, timeout, max_turns, tool_overrides=None):  # noqa: ARG001
        events.append(("agent", tool_overrides))
        if agent_error is not None:
            raise agent_error
        return CaseTrace(turns=2, usage={"total_tokens": 11}, duration_s=0.5)

    def fake_verify(row, image, base_repo, task_dir, patch, test_files):  # noqa: ARG001
        events.append("verify")
        return "resolved", {
            "test_patch_apply_rc": 0,
            "agent_patch_apply_rc": 0,
            "test_rc": 0,
            "docker_rc": 0,
        }

    monkeypatch.setattr(runner, "_ensure_image", lambda image: events.append("ensure"))
    monkeypatch.setattr(runner, "_extract_repo", fake_extract)
    monkeypatch.setattr(
        runner,
        "_prepare_base",
        lambda repo, commit, remote: events.append("prepare"),  # noqa: ARG005
    )
    monkeypatch.setattr(runner, "_start_bash_container", fake_start)
    monkeypatch.setattr(runner, "_stop_bash_container", fake_stop)
    monkeypatch.setattr(runner, "create_container_tools", lambda name: overrides)
    monkeypatch.setattr(runner, "_run_agent", fake_agent)
    monkeypatch.setattr(
        runner,
        "_collect_patch",
        lambda repo, commit: ("diff --git a/lib/a.py b/lib/a.py\n", ["lib/a.py"]),  # noqa: ARG005
    )
    monkeypatch.setattr(runner, "_verify", fake_verify)


def _event_kind(event):
    return event[0] if isinstance(event, tuple) else event


def test_run_instance_redacts_case_secrets_and_wraps_agent_in_container(tmp_path, monkeypatch):
    events: list = []
    overrides = {"bash": object(), "execute_command": object()}
    _install_run_instance_mocks(monkeypatch, events, overrides)
    row = _row()

    result = runner.run_instance(row, tmp_path, max_turns=30)

    assert [_event_kind(event) for event in events] == [
        "ensure",
        "prepare",
        "start",
        "agent",
        "stop",
        "verify",
    ]
    start = next(event for event in events if isinstance(event, tuple) and event[0] == "start")
    stop = next(event for event in events if isinstance(event, tuple) and event[0] == "stop")
    agent = next(event for event in events if isinstance(event, tuple) and event[0] == "agent")
    assert re.fullmatch(r"paicli-bash-[0-9a-f]{32}", start[1])
    assert start[2] == row["docker_image"]
    assert start[3] == tmp_path / row["instance_id"] / "repo"
    assert stop[1] == start[1]
    assert agent[1] is overrides

    task_dir = tmp_path / row["instance_id"]
    case = json.loads((task_dir / "case.json").read_text(encoding="utf-8"))
    assert case["gold_patch"] == "<redacted>"
    assert case["test_patch"] == "<redacted>"
    assert case["problem_statement"] == "fix the defect"
    assert case["base_commit"] == "abc123"
    for path in task_dir.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert "GOLD_SECRET_123" not in text
            assert "TEST_SECRET_456" not in text

    assert result["resolved"] is True
    assert result["status"] == "resolved"
    assert result["turns"] == 2
    assert result["total_tokens"] == 11


def test_run_instance_stops_container_when_agent_raises(tmp_path, monkeypatch):
    events: list = []
    overrides = {"bash": object(), "execute_command": object()}
    _install_run_instance_mocks(
        monkeypatch,
        events,
        overrides,
        agent_error=RuntimeError("agent blew up"),
    )

    with pytest.raises(RuntimeError, match="agent blew up"):
        runner.run_instance(_row(), tmp_path, max_turns=30)

    kinds = [_event_kind(event) for event in events]
    assert "stop" in kinds
    assert "verify" not in kinds


def test_start_bash_container_mounts_only_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list = []

    def fake_run(args, **kwargs):  # noqa: ARG001
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="true\n", stderr="")

    monkeypatch.setattr(runner, "_run", fake_run)

    name = runner._start_bash_container("image:tag", repo, "paicli-bash-abc")

    assert name == "paicli-bash-abc"
    run_args = calls[0]
    assert run_args[:5] == ["docker", "run", "-d", "--name", "paicli-bash-abc"]
    assert f"{repo}:/app" in run_args
    assert run_args[run_args.index("-w") + 1] == "/app"
    assert "--network=none" in run_args
    assert run_args[-3:] == ["image:tag", "sleep", "infinity"]
    assert run_args.count("-v") == 1
    assert str(tmp_path / "base_repo") not in " ".join(map(str, run_args))
    assert str(tmp_path / "task") not in " ".join(map(str, run_args))
    assert calls[1] == ["docker", "inspect", "-f", "{{.State.Running}}", "paicli-bash-abc"]


def _verify_row() -> dict:
    row = _row()
    row["fail_to_pass"] = json.dumps(["tests/test_a.py::test_x"])
    row["pass_to_pass"] = json.dumps(["tests/test_a.py::test_y"])
    return row


def _verify_junit(x_outcome="passed", y_outcome="passed") -> str:
    def case(name, outcome):
        body = {
            "failed": '<failure message="boom"/>',
            "skipped": '<skipped message="skip"/>',
        }.get(outcome, "")
        return (
            f'<testcase classname="tests.test_a" name="{name}" '
            f'file="tests/test_a.py" line="1">{body}</testcase>'
        )

    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="2">'
        + case("test_x", x_outcome)
        + case("test_y", y_outcome)
        + "</testsuite></testsuites>"
    )


def _install_verify_mocks(
    monkeypatch, task_dir, *, junit, test_rc=0, reset_rc=0, test_patch_rc=0, agent_rc=0
):
    def fake_run(args, **kwargs):  # noqa: ARG001
        if junit is not None:
            (task_dir / "junit.xml").write_text(junit, encoding="utf-8")
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                f"reset_rc={reset_rc}\n"
                f"test_patch_apply_rc={test_patch_rc}\n"
                f"agent_patch_apply_rc={agent_rc}\n"
                f"test_rc={test_rc}\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(runner, "_run", fake_run)


def test_verify_resolves_only_when_f2p_and_p2p_pass(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _install_verify_mocks(monkeypatch, task_dir, junit=_verify_junit())

    status, details = runner._verify(
        _verify_row(), "image:tag", tmp_path / "base", task_dir, "diff", ["tests/test_a.py"]
    )

    assert status == "resolved"
    assert details["f2p_passed"] == 1
    assert details["f2p_total"] == 1
    assert details["p2p_passed"] == 1
    assert details["p2p_total"] == 1
    script = (task_dir / "verify.sh").read_text(encoding="utf-8")
    assert "--junitxml=/task/junit.xml" in script


def test_verify_marks_failed_p2p_as_not_resolved(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _install_verify_mocks(monkeypatch, task_dir, junit=_verify_junit(y_outcome="failed"), test_rc=1)

    status, details = runner._verify(
        _verify_row(), "image:tag", tmp_path / "base", task_dir, "diff", ["tests/test_a.py"]
    )

    assert status == "not_resolved"
    assert details["f2p_passed"] == 1
    assert details["p2p_passed"] == 0
    assert details["p2p_failed"] == ["tests/test_a.py::test_y"]


def test_verify_marks_collection_error_separately(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    empty_junit = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="pytest" errors="1" tests="0"/></testsuites>'
    )
    _install_verify_mocks(monkeypatch, task_dir, junit=empty_junit, test_rc=2)

    status, details = runner._verify(
        _verify_row(), "image:tag", tmp_path / "base", task_dir, "diff", ["tests/test_a.py"]
    )

    assert status == "collection_error"
    assert details["collected_tests"] == 0
    assert details["f2p_passed"] == 0


def test_verify_treats_zero_collected_tests_as_collection_error(tmp_path, monkeypatch):
    # pytest 7.4 exits 1 when every selected module fails to import, so test_rc
    # alone cannot separate "broken imports" from "tests failed".
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    empty_junit = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="pytest" errors="1" tests="0"/></testsuites>'
    )
    _install_verify_mocks(monkeypatch, task_dir, junit=empty_junit, test_rc=1)

    status, details = runner._verify(
        _verify_row(), "image:tag", tmp_path / "base", task_dir, "diff", ["tests/test_a.py"]
    )

    assert status == "collection_error"
    assert details["collected_tests"] == 0


def test_verify_reports_empty_patch_instead_of_apply_failure(tmp_path, monkeypatch):
    # `git apply` on an empty diff exits 1, which would read as "the agent's
    # patch did not apply" instead of "the agent wrote no patch".
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _install_verify_mocks(monkeypatch, task_dir, junit=None, test_rc=1)

    status, details = runner._verify(
        _verify_row(), "image:tag", tmp_path / "base", task_dir, "", ["tests/test_a.py"]
    )

    assert status == "no_patch"
    assert details["agent_patch_apply_rc"] is None


def test_verify_reports_failed_base_checkout_as_infra_error(tmp_path, monkeypatch):
    # Without this guard a missing base commit leaves the working tree at some
    # other revision, and the patches either fail or grade the wrong code.
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _install_verify_mocks(monkeypatch, task_dir, junit=_verify_junit(), test_rc=0, reset_rc=128)

    status, details = runner._verify(
        _verify_row(), "image:tag", tmp_path / "base", task_dir, "diff", ["tests/test_a.py"]
    )

    assert status == "infra_error"
    assert details["reset_rc"] == 128
    script = (task_dir / "verify.sh").read_text(encoding="utf-8")
    assert "reset_rc=" in script


def _install_verify_sequence(monkeypatch, task_dir, *, first, second):
    """first/second: (junit_text_or_None, test_rc) for consecutive docker runs."""
    state = {"calls": 0}

    def fake_run(args, **kwargs):  # noqa: ARG001
        junit, test_rc = first if state["calls"] == 0 else second
        state["calls"] += 1
        if junit is not None:
            (task_dir / "junit.xml").write_text(junit, encoding="utf-8")
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                f"reset_rc=0\ntest_patch_apply_rc=0\nagent_patch_apply_rc=0\ntest_rc={test_rc}\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(runner, "_run", fake_run)
    return state


def test_verify_retries_with_node_ids_when_the_session_crashes(tmp_path, monkeypatch):
    # Some qutebrowser suites segfault on a test that is not graded; pytest dies
    # before writing junit, so every graded test loses its outcome.
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    state = _install_verify_sequence(
        monkeypatch, task_dir, first=(None, 139), second=(_verify_junit(), 0)
    )

    status, details = runner._verify(
        _verify_row(), "image:tag", tmp_path / "base", task_dir, "diff", ["tests/test_a.py"]
    )

    assert status == "resolved"
    assert details["test_fallback"] == "node_ids"
    assert state["calls"] == 2
    assert (task_dir / "nodeids.txt").read_text(encoding="utf-8").split() == [
        "tests/test_a.py::test_x",
        "tests/test_a.py::test_y",
    ]
    fallback = (task_dir / "verify_fallback.sh").read_text(encoding="utf-8")
    assert "/task/node_runner.py" in fallback
    assert "test_rc=$?" in fallback


def test_verify_keeps_the_file_level_run_when_it_produces_outcomes(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    state = _install_verify_sequence(
        monkeypatch, task_dir, first=(_verify_junit(), 0), second=(_verify_junit(), 0)
    )

    status, details = runner._verify(
        _verify_row(), "image:tag", tmp_path / "base", task_dir, "diff", ["tests/test_a.py"]
    )

    assert status == "resolved"
    assert details["test_fallback"] is None
    assert state["calls"] == 1
    assert not (task_dir / "nodeids.txt").exists()


def test_verify_reports_infra_error_when_the_retry_also_writes_no_junit(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _install_verify_sequence(monkeypatch, task_dir, first=(None, 139), second=(None, 139))

    status, details = runner._verify(
        _verify_row(), "image:tag", tmp_path / "base", task_dir, "diff", ["tests/test_a.py"]
    )

    assert status == "infra_error"
    assert details["test_fallback"] == "node_ids"


def test_stop_bash_container_ignores_failures(monkeypatch):
    calls: list = []

    def fake_run(args, **kwargs):  # noqa: ARG001
        calls.append(args)
        raise RuntimeError("docker unavailable")

    monkeypatch.setattr(runner, "_run", fake_run)

    runner._stop_bash_container("paicli-bash-abc")

    assert calls[0] == ["docker", "rm", "-f", "paicli-bash-abc"]


def test_run_instance_reports_agent_api_failure_as_infra_error(tmp_path, monkeypatch):
    # A 402/transport failure leaves no patch; calling that "no_patch" would
    # charge the model with producing nothing when the API never ran.
    events: list = []
    _install_run_instance_mocks(monkeypatch, events, {"bash": object()})

    def failing_agent(prompt, repo, *, timeout, max_turns, tool_overrides=None):  # noqa: ARG001
        return CaseTrace(error="deepseek API returned HTTP 402")

    monkeypatch.setattr(runner, "_run_agent", failing_agent)
    monkeypatch.setattr(runner, "_collect_patch", lambda repo, commit: ("", []))  # noqa: ARG005

    result = runner.run_instance(_row(), tmp_path, max_turns=30)

    assert result["status"] == "infra_error"
    assert result["resolved"] is False
    assert "402" in result["agent_error"]
