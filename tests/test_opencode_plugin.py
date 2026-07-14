"""Bun-level contract tests for the installed OpenCode plugin."""

import json
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(shutil.which("bun") is None, reason="Bun is required")


_FAKE_JCLI = r"""#!/usr/bin/env python3
import json
import os
import sys

payload = json.load(sys.stdin)
record = {"argv": sys.argv[1:], "payload": payload, "cwd": os.getcwd()}
with open(os.environ["JCLI_TEST_CALLS"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\n")

mode = os.environ.get("JCLI_TEST_MODE", "")
guard = sys.argv[2]
if mode == "failure":
    print("test failure", file=sys.stderr)
    sys.exit(7)
elif mode == "deny" and guard == "notebook-exec-guard":
    print(json.dumps({"hookSpecificOutput": {
        "permissionDecision": "deny",
        "permissionDecisionReason": "blocked by test",
    }}))
elif mode == "context" and guard == "pair-drift-guard-post":
    print(json.dumps({"hookSpecificOutput": {
        "additionalContext": "pair synced by test",
    }}))
elif mode == "invalid":
    print("{invalid")
"""


_RUNNER = r"""import { pathToFileURL } from "node:url"

const module = await import(pathToFileURL(process.env.JCLI_TEST_PLUGIN).href)
const logs = []
const hooks = await module.JcliPlugin({
  client: { app: { log: async (entry) => logs.push(entry) } },
  directory: process.env.JCLI_TEST_DIRECTORY,
})
const scenario = process.env.JCLI_TEST_SCENARIO

if (scenario === "mapping") {
  await hooks["tool.execute.before"](
    { tool: "bash", sessionID: "s", callID: "1" },
    { args: { command: "python analysis.py", workdir: "nested" } },
  )
  await hooks["tool.execute.before"](
    { tool: "edit", sessionID: "s", callID: "2" },
    { args: { filePath: "/tmp/analysis.py", oldString: "a", newString: "b" } },
  )
  const writeOutput = { title: "write", output: "written", metadata: {} }
  await hooks["tool.execute.after"](
    { tool: "write", sessionID: "s", callID: "3", args: { filePath: "/tmp/analysis.py" } },
    writeOutput,
  )
  await hooks["tool.execute.before"](
    { tool: "apply_patch", sessionID: "s", callID: "4" },
    { args: { patchText: "*** Begin Patch\n*** Update File: analysis.py\n*** End Patch" } },
  )
  const patchOutput = { title: "patch", output: "patched", metadata: {} }
  await hooks["tool.execute.after"](
    {
      tool: "apply_patch",
      sessionID: "s",
      callID: "5",
      args: { patchText: "*** Begin Patch\n*** Update File: analysis.py\n*** End Patch" },
    },
    patchOutput,
  )
  console.log(JSON.stringify({ logs, writeOutput, patchOutput }))
}

if (scenario === "deny") {
  let error
  try {
    await hooks["tool.execute.before"](
      { tool: "bash", sessionID: "s", callID: "1" },
      { args: { command: "jupyter nbconvert --execute analysis.ipynb" } },
    )
  } catch (caught) {
    error = String(caught)
  }
  console.log(JSON.stringify({ error, logs }))
}

if (scenario === "invalid") {
  await hooks["tool.execute.before"](
    { tool: "edit", sessionID: "s", callID: "1" },
    { args: { filePath: "/tmp/analysis.py" } },
  )
  console.log(JSON.stringify({ logs }))
}
"""


def _run_plugin(tmp_path: Path, scenario: str, mode: str = "") -> tuple[dict, list[dict]]:
    plugin = resources.files("jupyter_jcli").joinpath("opencode_plugin.js")
    fake_jcli = tmp_path / "fake-j-cli"
    fake_jcli.write_text(_FAKE_JCLI, encoding="utf-8")
    fake_jcli.chmod(0o755)
    runner = tmp_path / "runner.mjs"
    runner.write_text(_RUNNER, encoding="utf-8")
    calls_path = tmp_path / "calls.jsonl"
    directory = tmp_path / "project"
    (directory / "nested").mkdir(parents=True)

    env = {
        **os.environ,
        "JCLI_BIN": str(fake_jcli),
        "JCLI_TEST_CALLS": str(calls_path),
        "JCLI_TEST_DIRECTORY": str(directory),
        "JCLI_TEST_PLUGIN": str(plugin),
        "JCLI_TEST_SCENARIO": scenario,
        "JCLI_TEST_MODE": mode,
    }
    result = subprocess.run(
        ["bun", str(runner)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    output = json.loads(result.stdout)
    calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    return output, calls


def test_maps_tools_to_existing_guards(tmp_path):
    output, calls = _run_plugin(tmp_path, "mapping", mode="context")
    assert [call["argv"] for call in calls] == [
        ["_hooks", "notebook-exec-guard"],
        ["_hooks", "python-run-guard"],
        ["_hooks", "pair-drift-guard-pre"],
        ["_hooks", "pair-drift-guard-post"],
        ["_hooks", "pair-drift-guard-pre", "--platform", "codex"],
        ["_hooks", "pair-drift-guard-post", "--platform", "codex"],
    ]
    assert calls[0]["cwd"] == str(tmp_path / "project" / "nested")
    assert calls[0]["payload"]["cwd"] == str(tmp_path / "project" / "nested")
    assert calls[2]["payload"]["tool_input"]["file_path"] == "/tmp/analysis.py"
    assert calls[4]["payload"]["tool_input"]["command"][0] == "apply_patch"
    assert output["writeOutput"]["output"].endswith("pair synced by test")
    assert output["patchOutput"]["output"].endswith("pair synced by test")


def test_converts_deny_decision_to_tool_error(tmp_path):
    output, calls = _run_plugin(tmp_path, "deny", mode="deny")
    assert len(calls) == 1
    assert "blocked by test" in output["error"]


def test_invalid_guard_output_fails_open_and_logs(tmp_path):
    output, calls = _run_plugin(tmp_path, "invalid", mode="invalid")
    assert len(calls) == 1
    assert output["logs"][0]["body"]["level"] == "error"
    assert "invalid JSON" in output["logs"][0]["body"]["message"]


def test_nonzero_guard_exit_fails_open_and_logs(tmp_path):
    output, calls = _run_plugin(tmp_path, "invalid", mode="failure")
    assert len(calls) == 1
    assert output["logs"][0]["body"]["level"] == "error"
    assert "status 7" in output["logs"][0]["body"]["message"]
    assert output["logs"][0]["body"]["extra"]["stderr"] == "test failure"
