// Managed by j-cli setup dsh.

import { randomUUID } from 'node:crypto'

const DEFAULT_TIMEOUT_MS = 10_000
const MAX_TIMEOUT_MS = 120_000
const DEFAULT_DIAGNOSTIC_MAX_CHARS = 2_000
const MAX_DIAGNOSTIC_MAX_CHARS = 16_000
const MAX_TRANSPORT_BYTES = 32 * 1024 * 1024
const OUTPUT_STDOUT_MAX_BYTES = MAX_TRANSPORT_BYTES + 1024
const GENERIC_DENY_REASON = 'j-cli denied this tool call'
const TEXTUAL_MIME_TYPES = [
  'text/html',
  'text/markdown',
  'text/plain',
  'application/json',
  'image/svg+xml',
] as const
const RASTER_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif'])
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

type TextBlock = { type: 'text'; text: string }
type ImageAttachment = {
  attachmentId: string
  mediaType: string
  bytes: number
  width: number
  height: number
  name?: string
  originalDimensions?: { width: number; height: number }
}
type ContentBlock = TextBlock | { type: 'image'; attachment: ImageAttachment }
type UserMessage = {
  id: string
  role: 'user'
  content: TextBlock[]
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
  stdoutMaxBytes?: number
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
  inject?(services: string[], callback: (ctx: Context) => void): unknown
  on(event: string, listener: (...args: any[]) => unknown): unknown
}

type ToolRunContext = {
  agent?: Agent
  signal: AbortSignal
}

type OutputToolArgs = {
  file_path: string
  cell_index: number
  output_index?: number
  mime_type?: string
  offset?: number
  limit?: number
}

type AttachmentStore = {
  imageLimits?: { mediaTypes?: readonly string[] }
  saveImage(input: { data: Uint8Array; mediaType: string; name?: string }): Promise<ImageAttachment>
}

type ToolRegistry = {
  register(tool: Record<string, unknown>): () => void
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

function structuredContextText(stdout: string): string | undefined {
  const parsed = structuredOutput(stdout)
  const output = parsed?.hookSpecificOutput
  if (!isRecord(output) || typeof output.additionalContext !== 'string') return undefined
  const context = output.additionalContext.trim()
  return context.length === 0 ? undefined : context
}

function structuredContext(stdout: string, maxChars: number): string | undefined {
  const context = structuredContextText(stdout)
  return context === undefined ? undefined : clipped(context, maxChars)
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
  const context = structuredContextText(stdout)
  const repeatedDiagnostic = context === undefined ? '' : `${guard}: ${context}`
  let independentStderr = stderr
  if (repeatedDiagnostic && stderr === repeatedDiagnostic) {
    independentStderr = ''
  } else if (repeatedDiagnostic && stderr.endsWith(`\n${repeatedDiagnostic}`)) {
    independentStderr = stderr.slice(0, -(repeatedDiagnostic.length + 1)).trimEnd()
  }

  const pieces = [`jcli-dsh: ${guard} failed (${status})`]
  if (independentStderr) pieces.push(`stderr: ${independentStderr}`)
  if (detail) pieces.push(`error: ${detail}`)
  if (context !== undefined && independentStderr !== stderr) {
    pieces.push(context)
  } else if (stdout) {
    pieces.push(`stdout: ${stdout}`)
  }
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

function toolRegistry(ctx: Context): ToolRegistry | undefined {
  if (typeof ctx.get !== 'function') return undefined
  const candidate = ctx.get('tools')
  return isRecord(candidate) && typeof candidate.register === 'function'
    ? candidate as ToolRegistry
    : undefined
}

function attachmentStore(ctx: Context): AttachmentStore | undefined {
  if (typeof ctx.get !== 'function') return undefined
  const candidate = ctx.get('attachments')
  return isRecord(candidate) && typeof candidate.saveImage === 'function'
    ? candidate as AttachmentStore
    : undefined
}

function supportedMimeTypes(ctx: Context, explicit: string | undefined): string[] {
  const supported = new Set<string>(TEXTUAL_MIME_TYPES)
  const attachments = attachmentStore(ctx)
  if (attachments !== undefined) {
    const accepted = attachments.imageLimits?.mediaTypes
    for (const mimeType of accepted ?? RASTER_MIME_TYPES) {
      if (RASTER_MIME_TYPES.has(mimeType)) supported.add(mimeType)
    }
  }
  if (explicit !== undefined && (
    explicit.startsWith('text/')
    || explicit === 'image/svg+xml'
    || explicit === 'application/json'
    || explicit.endsWith('+json')
  )) {
    supported.add(explicit)
  }
  return [...supported]
}

function outputCommand(ctx: Context, config: NormalizedConfig, args: OutputToolArgs): string {
  const command = args.output_index === undefined ? 'outputs' : 'output'
  const parts = [
    quotePosix(config.executable),
    '-j',
    'notebook',
    command,
    quotePosix(args.file_path),
    '--cell',
    String(args.cell_index),
  ]
  if (args.output_index !== undefined) {
    parts.push('--output', String(args.output_index))
    if (args.mime_type !== undefined) parts.push('--mime', quotePosix(args.mime_type))
    if (args.offset !== undefined) parts.push('--offset', String(args.offset))
    if (args.limit !== undefined) parts.push('--limit', String(args.limit))
    for (const mimeType of supportedMimeTypes(ctx, args.mime_type)) {
      parts.push('--supported-mime', quotePosix(mimeType))
    }
  }
  return parts.join(' ')
}

function outputRequestFor(
  ctx: Context,
  exec: ToolRunContext,
  config: NormalizedConfig,
  args: OutputToolArgs,
): ShellRequest {
  const session = exec.agent?.session
  const request: ShellRequest = {
    command: outputCommand(ctx, config, args),
    timeoutMs: config.timeoutMs,
    stdoutMaxBytes: OUTPUT_STDOUT_MAX_BYTES,
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

function parseJsonRecord(value: string): Record<string, any> | undefined {
  if (value.length === 0) return undefined
  try {
    const parsed: unknown = JSON.parse(value)
    return isRecord(parsed) ? parsed : undefined
  } catch {
    return undefined
  }
}

function commandFailure(result: ShellRunResult): Error {
  const parsed = parseJsonRecord(outputText(result.stderr))
  const code = typeof parsed?.code === 'string' ? parsed.code : undefined
  const message = typeof parsed?.message === 'string' ? parsed.message : undefined
  const detail = code === undefined || message === undefined ? '' : `: ${code}: ${message}`
  return new Error(`jcli-dsh: read_notebook_output failed (${resultStatus(result)})${detail}`)
}

async function runOutputCommand(
  ctx: Context,
  config: NormalizedConfig,
  args: OutputToolArgs,
  exec: ToolRunContext,
): Promise<Record<string, any>> {
  throwIfAborted(exec.signal)
  let result: ShellRunResult
  try {
    const request = outputRequestFor(ctx, exec, config, args)
    result = await ctx.shell.run(ctx.shell.resolve(request))
  } catch (error: unknown) {
    if (exec.signal.aborted || isAbortError(error)) throw abortError()
    throw new Error(`jcli-dsh: read_notebook_output command failed: ${errorText(error)}`)
  }
  if (result.aborted || exec.signal.aborted) throw abortError()
  if (result.timedOut) throw new Error('jcli-dsh: read_notebook_output command timed out')
  if (result.exitCode !== 0) throw commandFailure(result)
  if (result.stdout?.truncated) {
    throw new Error('jcli-dsh: read_notebook_output response exceeded the trusted stdout budget')
  }
  const parsed = parseJsonRecord(outputText(result.stdout))
  if (parsed === undefined || parsed.status !== 'ok' || parsed.schema_version !== 1) {
    throw new Error('jcli-dsh: read_notebook_output returned an invalid JSON response')
  }
  return parsed
}

function decodeBase64(value: string): Uint8Array {
  if (value.length % 4 !== 0) throw new Error('invalid base64 length')
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
  const padding = value.endsWith('==') ? 2 : value.endsWith('=') ? 1 : 0
  const output = new Uint8Array(value.length / 4 * 3 - padding)
  let offset = 0
  for (let index = 0; index < value.length; index += 4) {
    const chunk = value.slice(index, index + 4)
    const values = [...chunk].map(character => character === '=' ? 0 : alphabet.indexOf(character))
    if (values.some(part => part < 0)) throw new Error('invalid base64 character')
    const bits = (values[0] << 18) | (values[1] << 12) | (values[2] << 6) | values[3]
    if (offset < output.length) output[offset++] = bits >> 16 & 0xff
    if (offset < output.length) output[offset++] = bits >> 8 & 0xff
    if (offset < output.length) output[offset++] = bits & 0xff
  }
  return output
}

function imageAttachment(value: unknown): ImageAttachment {
  if (!isRecord(value)
    || typeof value.attachmentId !== 'string'
    || typeof value.mediaType !== 'string'
    || typeof value.bytes !== 'number'
    || typeof value.width !== 'number'
    || typeof value.height !== 'number') {
    throw new Error('jcli-dsh: attachment save returned an invalid image reference')
  }
  return value as ImageAttachment
}

async function saveSelectedImage(
  ctx: Context,
  response: Record<string, any>,
): Promise<Record<string, any>> {
  const selected = response.selected
  if (!isRecord(selected)
    || selected.encoding !== 'base64'
    || typeof selected.mime_type !== 'string'
    || !RASTER_MIME_TYPES.has(selected.mime_type)) {
    return response
  }
  const attachments = attachmentStore(ctx)
  if (attachments === undefined) {
    throw new Error('jcli-dsh: cannot view this image because the host attachment service is unavailable')
  }
  if (typeof selected.data !== 'string') {
    throw new Error('jcli-dsh: image output did not contain valid base64 data')
  }
  let data: Uint8Array
  try {
    data = decodeBase64(selected.data)
  } catch {
    throw new Error('jcli-dsh: image output did not contain valid base64 data')
  }
  const attachment = imageAttachment(await attachments.saveImage({
    data,
    mediaType: selected.mime_type,
  }))
  const { data: _encoded, ...facts } = selected
  return {
    ...response,
    selected: {
      ...facts,
      encoding: 'attachment',
      bytes: attachment.bytes,
      attachment,
    },
  }
}

function outputProvenance(source: unknown, includeOutput: boolean): Record<string, any> {
  if (!isRecord(source)) return {}
  const provenance: Record<string, any> = {
    path: source.path,
    cell: source.cell_index,
  }
  if (includeOutput) provenance.output = source.output_index
  if (typeof source.mapping === 'string' && source.mapping !== 'direct') {
    provenance.requested = {
      path: source.requested_path,
      cell: source.requested_cell_index,
      mapping: source.mapping,
    }
  }
  return provenance
}

function outputPage(payload: unknown): Record<string, any> | undefined {
  if (!isRecord(payload) || payload.truncated !== true) return undefined
  return {
    offset: payload.offset,
    returned: payload.returned_characters,
    total: payload.total_characters,
    ...(payload.next_offset === undefined ? {} : { next_offset: payload.next_offset }),
  }
}

function renderMetadata(value: Record<string, any>): string {
  const selected = isRecord(value.selected) ? value.selected : undefined
  const stream = isRecord(value.stream) ? value.stream : undefined
  const available = Array.isArray(value.available_mime_types)
    ? value.available_mime_types
    : []
  const page = outputPage(selected ?? stream)
  const output = {
    type: value.output_type,
    ...(selected === undefined ? {} : { mime: selected.mime_type }),
    ...(available.length > 1 ? { available } : {}),
    ...(stream === undefined ? {} : { name: stream.name }),
    ...(page === undefined ? {} : { page }),
  }
  return JSON.stringify({
    provenance: outputProvenance(value.source, true),
    output,
  })
}

function renderDirectory(value: Record<string, any>): string {
  const outputs = Array.isArray(value.outputs)
    ? value.outputs.map((entry: unknown) => {
        if (!isRecord(entry)) return entry
        return {
          index: entry.output_index,
          type: entry.output_type,
          mime: Array.isArray(entry.available_mime_types) ? entry.available_mime_types : [],
          ...(entry.output_type === 'stream' ? { name: entry.name } : {}),
          ...(entry.output_type === 'error'
            ? { ename: entry.ename, evalue: entry.evalue }
            : {}),
        }
      })
    : []
  return JSON.stringify({
    provenance: outputProvenance(value.source, false),
    outputs,
  })
}

function renderOutput(_args: OutputToolArgs, value: Record<string, any>): ContentBlock[] {
  if (Array.isArray(value.outputs)) {
    return [{ type: 'text', text: renderDirectory(value) }]
  }
  const metadata = { type: 'text' as const, text: renderMetadata(value) }
  if (isRecord(value.selected)) {
    if (value.selected.encoding === 'attachment' && isRecord(value.selected.attachment)) {
      return [{ type: 'image', attachment: value.selected.attachment as ImageAttachment }, metadata]
    }
    if (value.selected.encoding === 'json') {
      return [{ type: 'text', text: JSON.stringify(value.selected.data) }, metadata]
    }
    if (typeof value.selected.data === 'string') {
      return [{ type: 'text', text: value.selected.data }, metadata]
    }
  }
  if (isRecord(value.stream) && typeof value.stream.data === 'string') {
    return [{ type: 'text', text: value.stream.data }, metadata]
  }
  if (isRecord(value.error)) {
    return [{ type: 'text', text: JSON.stringify(value.error) }, metadata]
  }
  return [metadata]
}

function validateOutputArgs(value: unknown): OutputToolArgs {
  const allowed = new Set(['file_path', 'cell_index', 'output_index', 'mime_type', 'offset', 'limit'])
  if (!isRecord(value)
    || Object.keys(value).some(key => !allowed.has(key))
    || typeof value.file_path !== 'string'
    || !Number.isInteger(value.cell_index)
    || (value.output_index !== undefined && !Number.isInteger(value.output_index))
    || (value.mime_type !== undefined && typeof value.mime_type !== 'string')
    || (value.offset !== undefined && !Number.isInteger(value.offset))
    || (value.limit !== undefined && !Number.isInteger(value.limit))
    || (value.output_index === undefined
      && (value.mime_type !== undefined || value.offset !== undefined || value.limit !== undefined))) {
    throw new Error('invalid arguments for read_notebook_output')
  }
  return value as OutputToolArgs
}

function registerOutputTool(ctx: Context, config: NormalizedConfig): void {
  const tools = toolRegistry(ctx)
  if (tools === undefined) {
    ctx.logger.warn('jcli-dsh: tools service is unavailable; read_notebook_output was not registered')
    return
  }
  tools.register({
    name: 'read_notebook_output',
    description: 'List saved outputs for one notebook cell, or read one output on demand without executing or modifying the notebook. Omit output_index to return only the output directory.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        file_path: { type: 'string', description: 'Notebook or paired percent-Python path.' },
        cell_index: { type: 'integer', description: 'Physical zero-based cell index in file_path.' },
        output_index: { type: 'integer', description: 'Physical zero-based output index. Omit to list outputs without payloads.' },
        mime_type: { type: 'string', description: 'Exact MIME representation to read.' },
        offset: { type: 'integer', description: 'Zero-based text character offset.' },
        limit: { type: 'integer', description: 'Maximum text characters to return.' },
      },
      required: ['file_path', 'cell_index'],
    },
    output: {
      schema: {},
      render: renderOutput,
    },
    isConcurrencySafe: () => true,
    async execute(value: unknown, exec: ToolRunContext) {
      const args = validateOutputArgs(value)
      const response = await runOutputCommand(ctx, config, args, exec)
      return saveSelectedImage(ctx, response)
    },
  })
}

export function apply(ctx: Context, config?: Config): void {
  ensureSandboxPolicy(ctx)
  const normalized = normalizeConfig(config)
  const register = (scope: Context): void => {
    try {
      registerOutputTool(scope, normalized)
    } catch (error: unknown) {
      ctx.logger.warn(`jcli-dsh: failed to register read_notebook_output: ${errorText(error)}`)
    }
  }
  if (typeof ctx.inject === 'function') ctx.inject(['tools'], register)
  else register(ctx)

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
