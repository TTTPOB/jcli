// Managed by j-cli setup opencode.

import path from "node:path"
import { tool } from "@opencode-ai/plugin"

const service = "j-cli"
const enabledCapabilities = {"hook":true,"tool":true}
const executable = process.env.JCLI_BIN || "j-cli"
const maxTransportBytes = 32 * 1024 * 1024
const maxStdoutBytes = maxTransportBytes + 1
const maxStderrBytes = 256 * 1024
const supportedMimeTypes = [
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
  "text/html",
  "text/markdown",
  "text/plain",
  "application/json",
  "image/svg+xml",
]

const readBounded = async (stream, maxBytes, label, onLimit) => {
  const reader = stream.getReader()
  const chunks = []
  let total = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      total += value.byteLength
      if (total > maxBytes) {
        onLimit()
        await reader.cancel()
        throw new Error(`${label} exceeded ${maxBytes} bytes`)
      }
      chunks.push(value)
    }
  } finally {
    reader.releaseLock()
  }

  const bytes = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.byteLength
  }
  return new TextDecoder().decode(bytes)
}

const cliErrorMessage = (stdout, stderr, exitCode) => {
  const diagnostic = stderr.trim()
  if (diagnostic) return diagnostic
  try {
    const error = JSON.parse(stdout)
    if (error?.code && error?.message) return `${error.code}: ${error.message}`
    if (error?.message) return error.message
  } catch {
    // Preserve the exit status diagnostic without echoing arbitrary output data.
  }
  return `j-cli exited with status ${exitCode}`
}

const runOutputCommand = async (argv, cwd, signal) => {
  if (signal.aborted) throw new Error("Notebook output read cancelled")
  let proc
  try {
    proc = Bun.spawn(argv, {
      cwd,
      stdout: "pipe",
      stderr: "pipe",
      signal,
    })
    const stop = () => proc.kill()
    const [stdout, stderr, exitCode] = await Promise.all([
      readBounded(proc.stdout, maxStdoutBytes, "j-cli stdout", stop),
      readBounded(proc.stderr, maxStderrBytes, "j-cli stderr", stop),
      proc.exited,
    ])

    if (signal.aborted) throw new Error("Notebook output read cancelled")
    if (exitCode !== 0) {
      throw new Error(`Notebook output read failed: ${cliErrorMessage(stdout, stderr, exitCode)}`)
    }
    if (!stdout.trim()) throw new Error("Notebook output read failed: j-cli returned no JSON")
    try {
      return JSON.parse(stdout)
    } catch (error) {
      throw new Error(`Notebook output read failed: invalid JSON (${String(error)})`)
    }
  } catch (error) {
    if (signal.aborted || error?.name === "AbortError") {
      throw new Error("Notebook output read cancelled")
    }
    throw error
  }
}

const provenanceMetadata = (response) => ({
  schema_version: response.schema_version,
  source: response.source,
})

const pageMetadata = (value) => {
  const { data: _data, ...metadata } = value
  return metadata
}

const outputResult = (args, response) => {
  const title = `Notebook output: ${args.file_path} cell ${args.cell_index}`
  const metadata = {
    ...provenanceMetadata(response),
    output_type: response.output_type,
    available_mime_types: response.available_mime_types,
  }

  if (response.stream) {
    return {
      title,
      output: response.stream.data,
      metadata: { ...metadata, stream: pageMetadata(response.stream) },
    }
  }
  if (response.error) {
    return {
      title,
      output: response.error.traceback.join("\n"),
      metadata: { ...metadata, error: response.error },
    }
  }

  const selected = response.selected
  if (!selected || typeof selected.mime_type !== "string") {
    throw new Error("Notebook output read failed: missing selected representation")
  }
  const selectedMetadata = pageMetadata(selected)
  const resultMetadata = {
    ...metadata,
    notebook_metadata: response.metadata,
    selected: selectedMetadata,
    ...(response.execution_count === undefined
      ? {}
      : { execution_count: response.execution_count }),
  }
  if (selected.encoding === "base64") {
    return {
      title,
      output: `[${selected.mime_type} notebook output attached]`,
      metadata: resultMetadata,
      attachments: [
        {
          type: "file",
          mime: selected.mime_type,
          url: `data:${selected.mime_type};base64,${selected.data}`,
          filename: path.basename(response.source.path),
        },
      ],
    }
  }
  return {
    title,
    output:
      selected.encoding === "json"
        ? JSON.stringify(selected.data)
        : selected.data,
    metadata: resultMetadata,
  }
}

const notebookOutputTool = tool({
  description:
    "List or read saved output from a notebook cell without executing or modifying the notebook.",
  args: {
    file_path: tool.schema.string().describe("Notebook or paired percent-format Python file"),
    cell_index: tool.schema.number().int().nonnegative().describe("Zero-based physical cell index"),
    output_index: tool.schema.number().int().nonnegative().optional().describe("Zero-based output index; omit to list outputs"),
    mime_type: tool.schema.string().optional().describe("Exact MIME representation to read"),
    offset: tool.schema.number().int().nonnegative().optional().describe("Character offset for text output"),
    limit: tool.schema.number().int().positive().optional().describe("Maximum characters for text output"),
  },
  async execute(args, context) {
    const resolvedPath = path.resolve(context.directory, args.file_path)
    await context.ask({
      permission: "read",
      patterns: [path.relative(context.worktree, resolvedPath)],
      always: ["*"],
      metadata: { path: resolvedPath },
    })

    const isDirectory = args.output_index === undefined
    const argv = [
      executable,
      "--json",
      "notebook",
      isDirectory ? "outputs" : "output",
      args.file_path,
      "--cell",
      String(args.cell_index),
    ]
    if (!isDirectory) {
      argv.push("--output", String(args.output_index))
      const capabilities =
        args.mime_type && !supportedMimeTypes.includes(args.mime_type)
          ? [...supportedMimeTypes, args.mime_type]
          : supportedMimeTypes
      for (const mimeType of capabilities) argv.push("--supported-mime", mimeType)
      if (args.mime_type !== undefined) argv.push("--mime", args.mime_type)
      if (args.offset !== undefined) argv.push("--offset", String(args.offset))
      if (args.limit !== undefined) argv.push("--limit", String(args.limit))
    }

    const response = await runOutputCommand(argv, context.directory, context.abort)
    if (response?.status !== "ok") {
      const code = response?.code ? `${response.code}: ` : ""
      throw new Error(`Notebook output read failed: ${code}${response?.message || "unexpected response status"}`)
    }
    if (isDirectory) {
      return {
        title: `Notebook outputs: ${args.file_path} cell ${args.cell_index}`,
        output: JSON.stringify(response.outputs),
        metadata: provenanceMetadata(response),
      }
    }
    return outputResult(args, response)
  },
})

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

  const plugin = {
    tool: enabledCapabilities.tool ? {
      read_notebook_output: notebookOutputTool,
    } : {},

    "tool.execute.before": async (input, output) => {
      if (!enabledCapabilities.hook) return

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
      if (!enabledCapabilities.hook) return
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
  return plugin
}
