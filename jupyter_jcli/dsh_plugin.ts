// Managed by j-cli setup dsh.

import { randomUUID } from 'node:crypto'

const DEFAULT_TIMEOUT_MS = 10_000
const MAX_TIMEOUT_MS = 120_000
const DEFAULT_DIAGNOSTIC_MAX_CHARS = 2_000
const MAX_DIAGNOSTIC_MAX_CHARS = 16_000
const GENERIC_DENY_REASON = 'j-cli denied this tool call'
const GUARDS = {
  notebook: 'notebook-exec-guard',
  python: 'python-run-guard',
  pairPre: 'pair-drift-guard-pre',
  pairPost: 'pair-drift-guard-post',
} as const

export const name = 'jcli-dsh'
export const inject = ['shell']

export interface Config {
  executable?: string
  timeoutMs?: number
  diagnosticMaxChars?: number
}

type ContentBlock = { type: 'text'; text: string }
type UserMessage = {
  id: string
  role: 'user'
  content: ContentBlock[]
  source: { kind: 'plugin'; plugin: string }
}

type Session = {
  id?: string
  header: { id?: string; cwd?: string }
}

type Agent = {
  session: Session
  inject(message: UserMessage): void
}

type ToolExecution = {
  callId: string
  name: string
  arguments: unknown
  agent?: Agent
  signal: AbortSignal
}

type PreToolDecision =
  | { kind: 'allow' }
  | { kind: 'deny'; reason: string }
  | { kind: 'ask'; reason?: string }

type PostToolDecision =
  | { kind: 'accept'; content?: unknown[]; value?: never; additionalContexts?: UserMessage[] }
  | { kind: 'accept'; value: unknown; content?: never; additionalContexts?: UserMessage[] }
  | { kind: 'block'; feedback: unknown[]; additionalContexts?: UserMessage[] }

type SandboxPolicy = Record<string, unknown>
type SandboxPolicyService = {
  resolve(request?: { session?: Session }): SandboxPolicy
}

type ShellRequest = {
  command: string
  workdir?: string
  timeoutMs?: number
  signal?: AbortSignal
  stdin?: string
  sandboxPolicy?: SandboxPolicy
}

type ShellOutput = { text?: string; truncated?: boolean; spillPath?: string }
type ShellRunResult = {
  exitCode?: number | null
  signal?: string | null
  timedOut?: boolean
  aborted?: boolean
  timeoutMs?: number
  stdout?: ShellOutput
  stderr?: ShellOutput
}

type ShellExecutor = {
  sandboxMode?: string
  resolve(request: ShellRequest): unknown
  run(spec: unknown): Promise<ShellRunResult>
}

type Logger = {
  warn(...args: unknown[]): void
}

type Context = {
  shell: ShellExecutor
  logger: Logger
  get?(key: string): unknown
  on(event: string, listener: (...args: any[]) => unknown): unknown
}

type GuardResult =
  | { kind: 'ok'; context?: string }
  | { kind: 'deny'; reason: string }
  | { kind: 'failure'; diagnostic: string }

type NormalizedConfig = {
  executable: string
  timeoutMs: number
  diagnosticMaxChars: number
}

function validatePositiveBound(label: string, value: unknown, max: number): number {
  if (!Number.isInteger(value) || (value as number) < 1 || (value as number) > max) {
    throw new Error(`jcli-dsh: ${label} must be an integer from 1 to ${max}`)
  }
  return value as number
}

function normalizeConfig(config: Config | undefined): NormalizedConfig {
  const input = config ?? {}
  if (typeof input !== 'object' || input === null || Array.isArray(input)) {
    throw new Error('jcli-dsh: config must be an object')
  }

  const executable = input.executable ?? (process.env.JCLI_BIN || 'j-cli')
  if (typeof executable !== 'string' || executable.trim().length === 0) {
    throw new Error('jcli-dsh: executable must be a non-empty string')
  }
  const timeoutMs = input.timeoutMs === undefined
    ? DEFAULT_TIMEOUT_MS
    : validatePositiveBound('timeoutMs', input.timeoutMs, MAX_TIMEOUT_MS)
  const diagnosticMaxChars = input.diagnosticMaxChars === undefined
    ? DEFAULT_DIAGNOSTIC_MAX_CHARS
    : validatePositiveBound('diagnosticMaxChars', input.diagnosticMaxChars, MAX_DIAGNOSTIC_MAX_CHARS)
  return { executable, timeoutMs, diagnosticMaxChars }
}

/** Quote one executable as one POSIX shell word without interpreting its contents. */
function quotePosix(value: string): string {
  if (/^[A-Za-z0-9_./-]+$/.test(value)) return value
  return `'${value.replaceAll("'", "'\\''")}'`
}

function commandFor(executable: string, guard: string): string {
  return `${quotePosix(executable)} _hooks ${guard} --platform dsh`
}

function isRecord(value: unknown): value is Record<string, any> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function outputText(output: ShellOutput | undefined): string {
  return typeof output?.text === 'string' ? output.text : ''
}

function clipped(value: string, maxChars: number): string {
  if (value.length <= maxChars) return value
  const suffix = `... (+${value.length - maxChars} chars)`
  if (suffix.length >= maxChars) return suffix.slice(0, maxChars)
  return `${value.slice(0, maxChars - suffix.length)}${suffix}`
}

function errorText(error: unknown): string {
  try {
    if (error instanceof Error) return error.message
    return String(error)
  } catch {
    return '<unprintable error>'
  }
}

function abortError(): Error {
  const error = new Error('jcli-dsh: guard execution cancelled')
  error.name = 'AbortError'
  return error
}

function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) throw abortError()
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

function structuredOutput(stdout: string): Record<string, any> | undefined {
  if (stdout.trim().length === 0) return undefined
  try {
    const value: unknown = JSON.parse(stdout)
    return isRecord(value) ? value : undefined
  } catch {
    return undefined
  }
}

function structuredDeny(stdout: string): string | undefined {
  const parsed = structuredOutput(stdout)
  const output = parsed?.hookSpecificOutput
  if (!isRecord(output) || output.permissionDecision !== 'deny') return undefined
  if (typeof output.permissionDecisionReason !== 'string') return GENERIC_DENY_REASON
  const reason = output.permissionDecisionReason.trim()
  return reason.length === 0 ? GENERIC_DENY_REASON : reason
}

function structuredContext(stdout: string, maxChars: number): string | undefined {
  const parsed = structuredOutput(stdout)
  const output = parsed?.hookSpecificOutput
  if (!isRecord(output) || typeof output.additionalContext !== 'string') return undefined
  const context = output.additionalContext.trim()
  return context.length === 0 ? undefined : clipped(context, maxChars)
}

function resultStatus(result: ShellRunResult): string {
  if (result.exitCode !== undefined && result.exitCode !== null) {
    return `exit code ${result.exitCode}`
  }
  if (result.signal) return `signal ${result.signal}`
  return 'no exit code'
}

function failureDiagnostic(
  guard: string,
  result: ShellRunResult | undefined,
  thrown: unknown,
  maxChars: number,
): string {
  const status = result === undefined ? 'no exit code' : resultStatus(result)
  const stderr = result === undefined ? '' : outputText(result.stderr).trim()
  const stdout = result === undefined ? '' : outputText(result.stdout).trim()
  const detail = thrown === undefined ? '' : errorText(thrown)
  const pieces = [`jcli-dsh: ${guard} failed (${status})`]
  if (stderr) pieces.push(`stderr: ${stderr}`)
  if (detail) pieces.push(`error: ${detail}`)
  if (stdout) pieces.push(`stdout: ${stdout}`)
  return clipped(pieces.join('; '), maxChars)
}

function makeContext(text: string): UserMessage {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'plugin', plugin: name },
  }
}

function prependContext(ours: UserMessage, downstream: PostToolDecision): PostToolDecision {
  const contexts = [ours, ...downstream.additionalContexts ?? []]
  return { ...downstream, additionalContexts: contexts }
}

function policyService(ctx: Context): SandboxPolicyService | undefined {
  if (typeof ctx.get !== 'function') return undefined
  const candidate = ctx.get('sandboxPolicy')
  return isRecord(candidate) && typeof candidate.resolve === 'function'
    ? candidate as SandboxPolicyService
    : undefined
}

function requestFor(
  ctx: Context,
  exec: ToolExecution,
  config: NormalizedConfig,
  guard: string,
  event: 'PreToolUse' | 'PostToolUse',
): ShellRequest {
  const session = exec.agent?.session
  const cwd = session?.header.cwd ?? process.cwd()
  const payload = {
    session_id: session?.header.id ?? session?.id ?? '',
    cwd,
    hook_event_name: event,
    tool_name: exec.name,
    tool_input: exec.arguments,
    tool_use_id: String(exec.callId),
  }
  const request: ShellRequest = {
    command: commandFor(config.executable, guard),
    stdin: JSON.stringify(payload),
    timeoutMs: config.timeoutMs,
    signal: exec.signal,
  }
  if (session?.header.cwd !== undefined) request.workdir = session.header.cwd

  const policy = policyService(ctx)
  if (policy !== undefined) {
    request.sandboxPolicy = exec.agent === undefined
      ? policy.resolve({})
      : policy.resolve({ session: exec.agent.session })
  }
  return request
}

async function runGuard(
  ctx: Context,
  exec: ToolExecution,
  config: NormalizedConfig,
  guard: string,
  event: 'PreToolUse' | 'PostToolUse',
  pre: boolean,
): Promise<GuardResult> {
  let result: ShellRunResult | undefined
  let thrown: unknown
  try {
    throwIfAborted(exec.signal)
    const request = requestFor(ctx, exec, config, guard, event)
    result = await ctx.shell.run(ctx.shell.resolve(request))
    if (result?.aborted) throw abortError()
    throwIfAborted(exec.signal)
  } catch (error: unknown) {
    if (exec.signal.aborted || isAbortError(error) || result?.aborted) throw abortError()
    thrown = error
  }

  if (thrown !== undefined || result === undefined || !isRecord(result)) {
    return {
      kind: 'failure',
      diagnostic: failureDiagnostic(guard, isRecord(result) ? result as ShellRunResult : undefined, thrown, config.diagnosticMaxChars),
    }
  }

  const stdout = outputText(result.stdout)
  const stderr = outputText(result.stderr).trim()
  const exitCode = result.exitCode
  if (exitCode === 0) {
    if (pre) {
      const reason = structuredDeny(stdout)
      if (reason !== undefined) return { kind: 'deny', reason: clipped(reason, config.diagnosticMaxChars) }
      return { kind: 'ok' }
    }
    return { kind: 'ok', context: structuredContext(stdout, config.diagnosticMaxChars) }
  }

  if (pre && exitCode === 2) {
    const reason = clipped(stderr || structuredDeny(stdout) || `j-cli ${guard} denied this tool call`, config.diagnosticMaxChars)
    return { kind: 'deny', reason }
  }

  return {
    kind: 'failure',
    diagnostic: failureDiagnostic(guard, result, undefined, config.diagnosticMaxChars),
  }
}

function logFailure(ctx: Context, diagnostic: string): void {
  ctx.logger.warn(diagnostic)
}

function injectFailure(ctx: Context, exec: ToolExecution, diagnostic: string): void {
  if (exec.agent === undefined) return
  try {
    exec.agent.inject(makeContext(diagnostic))
  } catch (error: unknown) {
    ctx.logger.warn(`jcli-dsh: failed to inject guard diagnostic: ${errorText(error)}`)
  }
}

function ensureSandboxPolicy(ctx: Context): void {
  const mode = ctx.shell.sandboxMode
  if (mode !== undefined && policyService(ctx) === undefined) {
    throw new Error('jcli-dsh: the mounted shell executor confines but ctx.sandboxPolicy is missing')
  }
}

export function apply(ctx: Context, config?: Config): void {
  ensureSandboxPolicy(ctx)
  const normalized = normalizeConfig(config)

  ctx.on('tools/pre-execute', async (exec: ToolExecution, next: () => Promise<PreToolDecision>): Promise<PreToolDecision> => {
    const guards = exec.name === 'bash'
      ? [GUARDS.notebook, GUARDS.python]
      : exec.name === 'edit' || exec.name === 'write'
        ? [GUARDS.pairPre]
        : []
    for (const guard of guards) {
      const outcome = await runGuard(ctx, exec, normalized, guard, 'PreToolUse', true)
      if (outcome.kind === 'deny') return { kind: 'deny', reason: outcome.reason }
      if (outcome.kind === 'failure') {
        logFailure(ctx, outcome.diagnostic)
        injectFailure(ctx, exec, outcome.diagnostic)
      }
    }
    throwIfAborted(exec.signal)
    return next()
  })

  ctx.on('tools/post-execute', async (
    exec: ToolExecution,
    _result: unknown,
    next: () => Promise<PostToolDecision>,
  ): Promise<PostToolDecision> => {
    if (exec.name !== 'edit' && exec.name !== 'write') return next()

    const outcome = await runGuard(ctx, exec, normalized, GUARDS.pairPost, 'PostToolUse', false)
    throwIfAborted(exec.signal)
    const downstream = await next()
    throwIfAborted(exec.signal)
    if (outcome.kind === 'ok' && outcome.context !== undefined) {
      return prependContext(makeContext(outcome.context), downstream)
    }
    if (outcome.kind === 'failure') {
      logFailure(ctx, outcome.diagnostic)
      return prependContext(makeContext(outcome.diagnostic), downstream)
    }
    return downstream
  })
}
