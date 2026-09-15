// Managed by j-cli setup opencode.

import path from "node:path"

const service = "j-cli"
const executable = process.env.JCLI_BIN || "j-cli"

export const JcliPlugin = async ({ client, directory }) => {
  const log = async (level, message, extra = {}) => {
    try {
      await client.app.log({
        body: { service, level, message, extra },
      })
    } catch {
      // Logging must not hide or replace the guard's own diagnostic.
    }
  }

  const runGuard = async (guard, payload, cwd, platform) => {
    const argv = [executable, "_hooks", guard]
    if (platform) argv.push("--platform", platform)

    try {
      const proc = Bun.spawn(argv, {
        cwd,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
      })
      proc.stdin.write(JSON.stringify(payload))
      proc.stdin.end()

      const [stdout, stderr, exitCode] = await Promise.all([
        new Response(proc.stdout).text(),
        new Response(proc.stderr).text(),
        proc.exited,
      ])
      const trimmedStdout = stdout.trim()
      const trimmedStderr = stderr.trim()

      // Exit status is authoritative. A deny must stay a deny even when a
      // broken producer writes non-JSON noise to stdout.
      if (exitCode === 2) {
        let hookOutput
        if (trimmedStdout) {
          try {
            hookOutput = JSON.parse(trimmedStdout)
          } catch (error) {
            await log("error", `${guard} returned invalid JSON with deny status`, {
              exitCode,
              error: String(error),
              stdout: trimmedStdout,
              stderr: trimmedStderr,
            })
          }
        }
        const reason =
          trimmedStderr ||
          hookOutput?.hookSpecificOutput?.permissionDecisionReason ||
          `${guard} denied this tool call`
        await log("error", `${guard} denied the tool call`, {
          exitCode,
          stderr: trimmedStderr,
          reason,
        })
        return { kind: "deny", exitCode, diagnostic: reason, output: hookOutput }
      }

      let hookOutput
      if (trimmedStdout) {
        try {
          hookOutput = JSON.parse(trimmedStdout)
        } catch (error) {
          const diagnostic = `${guard} returned invalid JSON: ${String(error)}`
          await log("error", diagnostic, {
            exitCode,
            stdout: trimmedStdout,
            stderr: trimmedStderr,
          })
          return { kind: "failure", exitCode: 1, diagnostic }
        }
      }

      if (exitCode !== 0) {
        const diagnostic =
          trimmedStderr || `${guard} failed with status ${exitCode}`
        await log("error", `${guard} failed with status ${exitCode}`, {
          exitCode,
          stderr: trimmedStderr,
        })
        return { kind: "failure", exitCode, diagnostic, output: hookOutput }
      }

      if (trimmedStderr) {
        await log("warn", `${guard} wrote to stderr`, { stderr: trimmedStderr })
      }
      return { kind: "ok", exitCode: 0, output: hookOutput }
    } catch (error) {
      const diagnostic = `failed to run ${guard}: ${String(error)}`
      await log("error", diagnostic)
      return { kind: "failure", exitCode: 1, diagnostic }
    }
  }

  const denyIfRequested = (result) => {
    if (result?.kind === "deny") {
      throw new Error(result.diagnostic || "j-cli denied this tool call")
    }
    if (result?.kind === "failure") {
      throw new Error(`j-cli guard failure: ${result.diagnostic}`)
    }
    const decision = result?.output?.hookSpecificOutput
    if (decision?.permissionDecision === "deny") {
      throw new Error(decision.permissionDecisionReason || "j-cli denied this tool call")
    }
  }

  const appendContext = (result, output) => {
    const context = result?.output?.hookSpecificOutput?.additionalContext
    const diagnostic =
      result?.kind === "failure" || result?.kind === "deny"
        ? result.diagnostic
        : undefined
    const text = [context, diagnostic && `j-cli guard diagnostic: ${diagnostic}`]
      .filter(Boolean)
      .join("\n\n")
    if (!text) return
    output.output = output.output ? `${output.output}\n\n${text}` : text
  }

  const editPayload = (tool, args) => ({
    hook_event_name: "PreToolUse",
    tool_name: tool,
    tool_input: { file_path: args.filePath },
    cwd: directory,
  })

  const patchPayload = (args, event) => ({
    hook_event_name: event,
    tool_name: "apply_patch",
    tool_input: { command: ["apply_patch", args.patchText] },
    cwd: directory,
  })

  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool === "bash") {
        const cwd = path.resolve(directory, output.args.workdir || ".")
        const payload = {
          hook_event_name: "PreToolUse",
          tool_name: "Bash",
          tool_input: { command: output.args.command },
          cwd,
        }
        denyIfRequested(await runGuard("notebook-exec-guard", payload, cwd))
        denyIfRequested(await runGuard("python-run-guard", payload, cwd))
        return
      }

      if (input.tool === "edit" || input.tool === "write") {
        denyIfRequested(await runGuard("pair-drift-guard-pre", editPayload(input.tool, output.args), directory))
        return
      }

      if (input.tool === "apply_patch") {
        denyIfRequested(
          await runGuard("pair-drift-guard-pre", patchPayload(output.args, "PreToolUse"), directory, "codex"),
        )
      }
    },

    "tool.execute.after": async (input, output) => {
      if (input.tool === "edit" || input.tool === "write") {
        const payload = editPayload(input.tool, input.args)
        payload.hook_event_name = "PostToolUse"
        appendContext(await runGuard("pair-drift-guard-post", payload, directory), output)
        return
      }

      if (input.tool === "apply_patch") {
        appendContext(
          await runGuard("pair-drift-guard-post", patchPayload(input.args, "PostToolUse"), directory, "codex"),
          output,
        )
      }
    },
  }
}
