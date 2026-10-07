// Shared host-side prefilter. Setup inlines this module for single-file deployment.
import { statSync as pairStat } from "node:fs"
import { resolve as pairResolve, extname as pairExtname } from "node:path"

export function needsPairGuardPayload(payload, phase) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return true
  const args = payload.tool_input
  if (typeof payload.tool_name !== "string" || !args || typeof args !== "object" || Array.isArray(args)) return true
  if (payload.cwd !== undefined && typeof payload.cwd !== "string") return true
  if (args.workdir !== undefined && typeof args.workdir !== "string") return true
  if (args.file_path !== undefined && typeof args.file_path !== "string") return true
  if (args.command !== undefined && typeof args.command !== "string" && !(Array.isArray(args.command) && args.command.every(item => typeof item === "string"))) return true
  let paths
  if (payload.tool_name === "apply_patch") {
    let command = args.command
    if (Array.isArray(command)) {
      if (command.length !== 2 || command[0] !== "apply_patch") return true
      command = command[1]
    }
    if (typeof command !== "string") return true
    const directive = /^(?:\*{3}|\*{2}_) (?:Update|Add|Delete) File: |^(?:\*{3}|\*{2}_) Move to: /
    paths = []
    for (const line of command.split(/\r?\n/)) {
      const match = directive.exec(line)
      if (match) paths.push(line.slice(match[0].length).trim())
    }
    if (!paths.length) return true
  } else if (["Edit", "Write", "edit", "write"].includes(payload.tool_name)) {
    paths = [args.file_path]
  } else return true
  for (const value of paths) {
    if (typeof value !== "string" || !value.trim() || value.includes("\0")) return true
    let file
    try {
      file = pairResolve(payload.cwd || process.cwd(), args.workdir || ".", value)
    } catch (error) {
      if (error instanceof TypeError || typeof error.code === "string") return true
      throw error
    }
    const suffix = pairExtname(file)
    if (phase === "pre" && suffix === ".ipynb") return true
    if (suffix !== ".py") continue
    try {
      pairStat(file.slice(0, -3) + ".ipynb")
    } catch (error) {
      if (error.code === "ENOENT" || error.code === "ENOTDIR") continue
      return true
    }
    return true
  }
  return false
}
