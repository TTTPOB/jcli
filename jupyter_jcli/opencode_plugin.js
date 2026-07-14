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
      // Logging must not turn a fail-open guard error into a tool failure.
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

      if (exitCode !== 0) {
        await log("error", `${guard} exited with status ${exitCode}`, { stderr: stderr.trim() })
        return undefined
      }
      if (stderr.trim()) {
        await log("warn", `${guard} wrote to stderr`, { stderr: stderr.trim() })
      }
      if (!stdout.trim()) return undefined

      try {
        return JSON.parse(stdout)
      } catch (error) {
        await log("error", `${guard} returned invalid JSON`, {
          error: String(error),
          stdout: stdout.trim(),
        })
        return undefined
      }
    } catch (error) {
      await log("error", `failed to run ${guard}`, { error: String(error) })
      return undefined
    }
  }

  const denyIfRequested = (result) => {
    const output = result?.hookSpecificOutput
    if (output?.permissionDecision === "deny") {
      throw new Error(output.permissionDecisionReason || "j-cli denied this tool call")
    }
  }

  const appendContext = (result, output) => {
    const context = result?.hookSpecificOutput?.additionalContext
    if (!context) return
    output.output = output.output ? `${output.output}\n\n${context}` : context
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
