import assert from 'node:assert/strict'
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import test from 'node:test'
import { apply, inject, name } from '../../jupyter_jcli/dsh_plugin.ts'

const pluginPath = new URL('../../jupyter_jcli/dsh_plugin.ts', import.meta.url)

function result(exitCode = 0, stdout = '', stderr = '', extras = {}) {
  return {
    exitCode,
    signal: null,
    timedOut: false,
    aborted: false,
    timeoutMs: 1000,
    stdout: { text: stdout },
    stderr: { text: stderr },
    ...extras,
  }
}

function exec(name, args = {}, options = {}) {
  return {
    callId: options.callId ?? 'call-1',
    name,
    arguments: args,
    signal: options.signal ?? new AbortController().signal,
    ...(options.agent === undefined ? {} : { agent: options.agent }),
  }
}

function makeAgent(cwd = '/session/cwd', id = 'session-1') {
  const injected = []
  return {
    session: { header: { cwd, id } },
    injected,
    inject(message) {
      injected.push(message)
    },
  }
}

function harness({ results = [], shellMode, policy, attachments, config, shellRun, get } = {}) {
  const listeners = new Map()
  const calls = []
  const logs = []
  const tools = []
  let index = 0
  const shell = {
    ...(shellMode === undefined ? {} : { sandboxMode: shellMode }),
    resolve(request) {
      calls.push(request)
      return request
    },
    async run(spec) {
      if (shellRun) return shellRun(spec)
      const next = results[index++]
      if (next instanceof Error) throw next
      return next ?? result()
    },
  }
  const ctx = {
    shell,
    tools: {
      register(tool) {
        tools.push(tool)
        return () => tools.splice(tools.indexOf(tool), 1)
      },
    },
    logger: { warn(message) { logs.push(String(message)) } },
    get(key) {
      if (get) return get(key)
      if (key === 'sandboxPolicy') return policy
      if (key === 'attachments') return attachments
      if (key === 'tools') return ctx.tools
      return undefined
    },
    on(event, listener) {
      listeners.set(event, listener)
      return () => listeners.delete(event)
    },
  }
  apply(ctx, config)
  return {
    pre: listeners.get('tools/pre-execute'),
    post: listeners.get('tools/post-execute'),
    calls,
    logs,
    shell,
    tools,
  }
}

function textOf(message) {
  return message.content.map(block => block.text).join('')
}

function context(plugin = 'downstream', text = 'downstream') {
  return {
    id: `${plugin}-id`,
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'plugin', plugin },
  }
}

test('exports the native plugin contract and imports without DSH packages', () => {
  assert.equal(name, 'jcli-dsh')
  assert.deepEqual(inject, ['shell'])
  assert.equal(typeof apply, 'function')
  assert.ok(pluginPath.pathname.endsWith('/jupyter_jcli/dsh_plugin.ts'))
})

test('filters unrelated tools without invoking a guard', async () => {
  const h = harness({ results: [result(9, '', 'must not run')] })
  let nextCalls = 0
  const decision = await h.pre(exec('read', { file_path: 'x.py' }), async () => {
    nextCalls++
    return { kind: 'allow' }
  })
  assert.deepEqual(decision, { kind: 'allow' })
  assert.equal(nextCalls, 1)
  assert.equal(h.calls.length, 0)
})

test('runs both bash guards with Claude-shaped stdin and session policy', async () => {
  const agent = makeAgent('/session/cwd', 'session-id')
  const policyRequests = []
  const policy = {
    resolve(request) {
      policyRequests.push(request)
      return { mode: 'workspace-write', workspaceRoot: '/policy/root', sessionId: 'session-id' }
    },
  }
  const h = harness({
    results: [result(), result()],
    shellMode: 'workspace-write',
    policy,
    config: { timeoutMs: 3456 },
  })
  let nextCalls = 0
  const signal = new AbortController().signal
  const decision = await h.pre(exec('bash', {
    command: 'echo hello',
    description: 'run command',
    workdir: 'ignored-by-plugin',
  }, { agent, signal, callId: 'bash-call' }), async () => {
    nextCalls++
    return { kind: 'allow' }
  })
  assert.deepEqual(decision, { kind: 'allow' })
  assert.equal(nextCalls, 1)
  assert.deepEqual(policyRequests, [
    { session: agent.session },
    { session: agent.session },
  ])
  assert.equal(h.calls.length, 2)
  assert.deepEqual(h.calls.map(call => call.command), [
    'j-cli _hooks notebook-exec-guard --platform dsh',
    'j-cli _hooks python-run-guard --platform dsh',
  ])
  for (const call of h.calls) {
    assert.equal(call.workdir, '/session/cwd')
    assert.equal(call.timeoutMs, 3456)
    assert.equal(call.signal, signal)
    assert.deepEqual(call.sandboxPolicy, {
      mode: 'workspace-write', workspaceRoot: '/policy/root', sessionId: 'session-id',
    })
    const payload = JSON.parse(call.stdin)
    assert.deepEqual(payload, {
      session_id: 'session-id',
      cwd: '/session/cwd',
      hook_event_name: 'PreToolUse',
      tool_name: 'bash',
      tool_input: { command: 'echo hello', description: 'run command', workdir: 'ignored-by-plugin' },
      tool_use_id: 'bash-call',
    })
  }
})

test('agentless calls resolve policy with an empty request and omit workdir', async () => {
  const requests = []
  const policy = { resolve(request) { requests.push(request); return { mode: 'read-only', workspaceRoot: '/policy' } } }
  const h = harness({ results: [result()], policy })
  await h.pre(exec('write', { file_path: 'x.py', content: 'x' }), async () => ({ kind: 'allow' }))
  assert.deepEqual(requests, [{}])
  assert.equal(Object.hasOwn(h.calls[0], 'workdir'), false)
  assert.equal(JSON.parse(h.calls[0].stdin).cwd, process.cwd())
})

test('confined shell without sandbox policy fails at plugin load', () => {
  assert.throws(
    () => harness({ shellMode: 'workspace-write' }),
    /ctx\.sandboxPolicy is missing/,
  )
})

test('pre exit 2 denies with stderr priority and skips later bash guard', async () => {
  const h = harness({ results: [result(2, JSON.stringify({ hookSpecificOutput: {
    permissionDecision: 'deny', permissionDecisionReason: 'stdout reason',
  } }), 'stderr reason')] })
  let nextCalls = 0
  const decision = await h.pre(exec('bash', { command: 'bad', description: 'bad' }), async () => {
    nextCalls++
    return { kind: 'allow' }
  })
  assert.deepEqual(decision, { kind: 'deny', reason: 'stderr reason' })
  assert.equal(nextCalls, 0)
  assert.equal(h.calls.length, 1)
})

test('pre exit 0 accepts the existing structured deny shape', async () => {
  const h = harness({ results: [result(0, JSON.stringify({ hookSpecificOutput: {
    permissionDecision: 'deny', permissionDecisionReason: 'structured reason',
  } }))] })
  const decision = await h.pre(exec('write', { file_path: 'x.py' }), async () => ({ kind: 'allow' }))
  assert.deepEqual(decision, { kind: 'deny', reason: 'structured reason' })
})

test('pre exit 0 preserves structured deny when its reason is missing or empty', async () => {
  for (const hookSpecificOutput of [
    { permissionDecision: 'deny' },
    { permissionDecision: 'deny', permissionDecisionReason: '' },
  ]) {
    const h = harness({ results: [result(0, JSON.stringify({ hookSpecificOutput }))] })
    const decision = await h.pre(exec('write', { file_path: 'x.py' }), async () => ({ kind: 'allow' }))
    assert.equal(decision.kind, 'deny')
    assert.equal(decision.reason, 'j-cli denied this tool call')
  }
})

test('pre failures fail open, log, inject bounded diagnostics, and continue guards', async () => {
  const longStderr = 'S'.repeat(500)
  const agent = makeAgent()
  const h = harness({
    results: [result(1, '', longStderr), result()],
    config: { diagnosticMaxChars: 80 },
  })
  let nextCalls = 0
  const decision = await h.pre(exec('bash', { command: 'x', description: 'x' }, { agent }), async () => {
    nextCalls++
    return { kind: 'allow' }
  })
  assert.deepEqual(decision, { kind: 'allow' })
  assert.equal(nextCalls, 1)
  assert.equal(h.calls.length, 2)
  assert.equal(agent.injected.length, 1)
  assert.equal(textOf(agent.injected[0]).length <= 80, true)
  assert.match(textOf(agent.injected[0]), /exit code 1/)
  assert.match(h.logs[0], /exit code 1/)
})

test('pre exceptions fail open with an injected diagnostic', async () => {
  const agent = makeAgent()
  const h = harness({ results: [new Error('executor unavailable'), result()] })
  const decision = await h.pre(exec('bash', { command: 'x' }, { agent }), async () => ({ kind: 'allow' }))
  assert.deepEqual(decision, { kind: 'allow' })
  assert.equal(agent.injected.length, 1)
  assert.match(textOf(agent.injected[0]), /executor unavailable/)
  assert.match(h.logs[0], /executor unavailable/)
})

test('cancellation from the shell is propagated and does not call next', async () => {
  const controller = new AbortController()
  const h = harness({ results: [result(null, '', '', { aborted: true })] })
  let nextCalls = 0
  await assert.rejects(
    h.pre(exec('write', { file_path: 'x.py' }, { signal: controller.signal }), async () => {
      nextCalls++
      return { kind: 'allow' }
    }),
    error => error.name === 'AbortError',
  )
  assert.equal(nextCalls, 0)
})

test('post success prepends context while preserving downstream block, feedback, and contexts', async () => {
  const h = harness({ results: [result(0, JSON.stringify({ hookSpecificOutput: {
    additionalContext: 'pair synced',
  } }))] })
  const downstream = {
    kind: 'block',
    feedback: [{ type: 'text', text: 'downstream feedback' }],
    additionalContexts: [context('downstream', 'existing context')],
  }
  const decision = await h.post(
    exec('edit', { file_path: 'x.py', old_string: 'a', new_string: 'b' }),
    { isError: false },
    async () => downstream,
  )
  assert.equal(decision.kind, 'block')
  assert.deepEqual(decision.feedback, downstream.feedback)
  assert.equal(decision.additionalContexts.length, 2)
  assert.equal(textOf(decision.additionalContexts[0]), 'pair synced')
  assert.deepEqual(decision.additionalContexts[1], downstream.additionalContexts[0])
  assert.deepEqual(decision.additionalContexts[0].source, { kind: 'plugin', plugin: 'jcli-dsh' })
})

test('post failures never block and prepend bounded real process diagnostics', async () => {
  const h = harness({
    results: [result(null, '', 'post conflict details', { signal: 'SIGTERM' })],
    config: { diagnosticMaxChars: 100 },
  })
  let nextCalls = 0
  const downstream = {
    kind: 'accept',
    value: { ok: true },
    additionalContexts: [context('downstream')],
  }
  const decision = await h.post(exec('write', { file_path: 'x.py', content: 'x' }), {}, async () => {
    nextCalls++
    return downstream
  })
  assert.equal(nextCalls, 1)
  assert.equal(decision.kind, 'accept')
  assert.deepEqual(decision.value, { ok: true })
  assert.equal(decision.additionalContexts.length, 2)
  assert.match(textOf(decision.additionalContexts[0]), /signal SIGTERM/)
  assert.match(textOf(decision.additionalContexts[0]), /post conflict details/)
  assert.equal(textOf(decision.additionalContexts[0]).length <= 100, true)
})

test('post guard exceptions still delegate downstream', async () => {
  const h = harness({ results: [new Error('post executor failure')] })
  const downstream = { kind: 'accept', content: [{ type: 'text', text: 'tool result' }] }
  const decision = await h.post(exec('edit', { file_path: 'x.py' }), {}, async () => downstream)
  assert.equal(decision.kind, 'accept')
  assert.deepEqual(decision.content, downstream.content)
  assert.equal(decision.additionalContexts.length, 1)
  assert.match(textOf(decision.additionalContexts[0]), /post executor failure/)
})

test('real POSIX shell validates executable quoting for spaces, apostrophes, and metacharacters', async () => {
  const root = mkdtempSync(join(tmpdir(), 'jcli dsh '))
  const executable = join(root, "fake j-cli's [test]")
  writeFileSync(executable, '#!/bin/sh\ncat >/dev/null\nprintf "{}"\n', 'utf8')
  chmodSync(executable, 0o755)
  const shell = {
    sandboxMode: undefined,
    resolve(request) { return request },
    async run(spec) {
      const child = spawnSync('/bin/bash', ['-c', spec.command], {
        cwd: root,
        input: spec.stdin,
        encoding: 'utf8',
      })
      return result(child.status, child.stdout, child.stderr, {
        signal: child.signal,
      })
    },
  }
  const h = harness({ shellRun: spec => shell.run(spec), config: { executable } })
  const decision = await h.pre(exec('write', { file_path: 'x.py', content: 'x' }), async () => ({ kind: 'allow' }))
  assert.deepEqual(decision, { kind: 'allow' })
  assert.equal(h.calls.length, 1)
  assert.equal(h.calls[0].command.startsWith("'") , true)
})

test('registers read_notebook_output and lists without requesting payload data', async () => {
  const response = {
    schema_version: 1,
    status: 'ok',
    source: { kind: 'notebook', path: '/session/cwd/book.ipynb', cell_index: 2, mapping: 'direct' },
    outputs: [
      { output_index: 0, output_type: 'stream', available_mime_types: [], name: 'stdout' },
      { output_index: 1, output_type: 'display_data', available_mime_types: ['text/plain'], metadata: {} },
      { output_index: 2, output_type: 'error', available_mime_types: [], ename: 'ValueError', evalue: 'bad' },
    ],
  }
  const policy = { resolve() { return { mode: 'read-only', workspaceRoot: '/session/cwd' } } }
  const h = harness({ results: [result(0, JSON.stringify(response))], policy, shellMode: 'read-only' })
  assert.equal(h.tools.length, 1)
  const tool = h.tools[0]
  assert.equal(tool.name, 'read_notebook_output')
  assert.deepEqual(tool.output.schema, {})

  const agent = makeAgent()
  const signal = new AbortController().signal
  const value = await tool.execute({ file_path: 'book.ipynb', cell_index: 2 }, { agent, signal })
  assert.deepEqual(value, response)
  assert.equal(h.calls.length, 1)
  assert.equal(h.calls[0].workdir, '/session/cwd')
  assert.equal(h.calls[0].signal, signal)
  assert.equal(h.calls[0].stdoutMaxBytes, 32 * 1024 * 1024 + 1024)
  assert.equal(h.calls[0].command, 'j-cli -j notebook outputs book.ipynb --cell 2')
  assert.doesNotMatch(h.calls[0].command, /--output|--supported-mime/)
  assert.deepEqual(tool.output.render({}, value), [{
    type: 'text',
    text: JSON.stringify({
      provenance: { path: '/session/cwd/book.ipynb', cell: 2 },
      outputs: [
        { index: 0, type: 'stream', mime: [], name: 'stdout' },
        { index: 1, type: 'display_data', mime: ['text/plain'] },
        { index: 2, type: 'error', mime: [], ename: 'ValueError', evalue: 'bad' },
      ],
    }),
  }])
  await assert.rejects(
    tool.execute({ file_path: 'book.ipynb', cell_index: 2, mime_type: 'text/plain' }, { signal }),
    /invalid arguments/,
  )
  assert.equal(h.calls.length, 1)
})

test('renders raw text, reversible JSON, stream, and error payloads with separate provenance', async () => {
  const h = harness()
  const tool = h.tools[0]
  const source = {
    kind: 'notebook', path: '/work/book.ipynb', cell_index: 0, output_index: 1,
    requested_path: '/work/book.ipynb', requested_cell_index: 0, mapping: 'direct',
    cell_id: 'cell-id', execution_count: 7,
  }
  const cases = [
    {
      value: { schema_version: 1, status: 'ok', source, output_type: 'display_data', available_mime_types: ['text/html'], selected: { mime_type: 'text/html', encoding: 'utf-8', data: '<b>raw</b>', bytes: 10 } },
      expected: '<b>raw</b>',
    },
    {
      value: { schema_version: 1, status: 'ok', source, output_type: 'display_data', available_mime_types: ['application/json'], selected: { mime_type: 'application/json', encoding: 'json', data: { answer: 42 }, bytes: 13 } },
      expected: '{"answer":42}',
    },
    {
      value: { schema_version: 1, status: 'ok', source, output_type: 'stream', available_mime_types: [], stream: { name: 'stderr', data: 'raw stream\n', bytes: 11 } },
      expected: 'raw stream\n',
    },
    {
      value: { schema_version: 1, status: 'ok', source, output_type: 'error', available_mime_types: [], error: { ename: 'ValueError', evalue: 'bad', traceback: ['line'] } },
      expected: '{"ename":"ValueError","evalue":"bad","traceback":["line"]}',
    },
  ]
  for (const { value, expected } of cases) {
    const blocks = tool.output.render({}, value)
    assert.equal(blocks[0].type, 'text')
    assert.equal(blocks[0].text, expected)
    const metadata = JSON.parse(blocks[1].text)
    assert.deepEqual(metadata.provenance, { path: '/work/book.ipynb', cell: 0, output: 1 })
    assert.equal(metadata.output.type, value.output_type)
    if (value.selected) assert.equal(metadata.output.mime, value.selected.mime_type)
    if (value.stream) assert.equal(metadata.output.name, 'stderr')
    assert.equal(blocks[1].text.includes(expected), false)
    assert.equal(blocks[1].text.includes('schema_version'), false)
    assert.equal(blocks[1].text.includes('requested_path'), false)
  }
})

test('projects mapped provenance, multiple MIME choices, and protocol paging facts', () => {
  const tool = harness().tools[0]
  const source = {
    kind: 'notebook', path: '/work/book.ipynb', cell_index: 4, output_index: 2,
    requested_path: '/work/book.py', requested_cell_index: 3, mapping: 'cell-id',
  }
  const base = {
    schema_version: 1,
    status: 'ok',
    source,
    output_type: 'display_data',
    available_mime_types: ['text/plain', 'text/html'],
  }
  const first = tool.output.render({}, {
    ...base,
    selected: {
      mime_type: 'text/plain', encoding: 'utf-8', data: '😀a', bytes: 5,
      offset: 0, returned_characters: 2, total_characters: 5, truncated: true, next_offset: 2,
    },
  })
  const last = tool.output.render({}, {
    ...base,
    selected: {
      mime_type: 'text/plain', encoding: 'utf-8', data: 'xyz', bytes: 3,
      offset: 2, returned_characters: 3, total_characters: 5, truncated: true,
    },
  })

  assert.deepEqual(JSON.parse(first[1].text), {
    provenance: {
      path: '/work/book.ipynb', cell: 4, output: 2,
      requested: { path: '/work/book.py', cell: 3, mapping: 'cell-id' },
    },
    output: {
      type: 'display_data', mime: 'text/plain', available: ['text/plain', 'text/html'],
      page: { offset: 0, returned: 2, total: 5, next_offset: 2 },
    },
  })
  assert.deepEqual(JSON.parse(last[1].text).output.page, {
    offset: 2, returned: 3, total: 5,
  })
})

test('saves an explicitly read raster through the host attachment bridge', async () => {
  const saved = []
  const attachments = {
    imageLimits: { mediaTypes: ['image/png'] },
    async saveImage(input) {
      saved.push(input)
      return {
        attachmentId: 'sha256:image', mediaType: 'image/png', bytes: input.data.byteLength,
        width: 1, height: 1,
      }
    },
  }
  const response = {
    schema_version: 1,
    status: 'ok',
    source: { kind: 'notebook', path: '/work/book.ipynb', cell_index: 0, output_index: 1, mapping: 'direct' },
    output_type: 'display_data',
    available_mime_types: ['image/png', 'text/plain'],
    metadata: {},
    selected: { mime_type: 'image/png', encoding: 'base64', bytes: 8, data: 'iVBORw0KGgo=' },
  }
  const h = harness({ results: [result(0, JSON.stringify(response))], attachments })
  const value = await h.tools[0].execute({
    file_path: '/work/book.ipynb', cell_index: 0, output_index: 1, mime_type: 'image/png',
  }, { signal: new AbortController().signal })
  assert.match(h.calls[0].command, /--supported-mime image\/png/)
  assert.equal(saved.length, 1)
  assert.deepEqual([...saved[0].data], [137, 80, 78, 71, 13, 10, 26, 10])
  assert.equal(Object.hasOwn(saved[0], 'name'), false)
  assert.equal(value.selected.encoding, 'attachment')
  assert.equal(Object.hasOwn(value.selected, 'data'), false)
  assert.equal(JSON.stringify(value).includes('iVBORw0KGgo='), false)
  const blocks = h.tools[0].output.render({}, value)
  assert.deepEqual(blocks[0], { type: 'image', attachment: value.selected.attachment })
  assert.deepEqual(JSON.parse(blocks[1].text), {
    provenance: { path: '/work/book.ipynb', cell: 0, output: 1 },
    output: {
      type: 'display_data', mime: 'image/png', available: ['image/png', 'text/plain'],
    },
  })
})

test('checks cancellation, timeout, exit, and truncation before parsing stdout', async () => {
  const encoded = 'iVBORw0KGgo='
  const scenarios = [
    [result(0, encoded, '', { aborted: true }), error => error.name === 'AbortError'],
    [result(0, encoded, '', { timedOut: true }), /timed out/],
    [result(1, encoded, JSON.stringify({ status: 'error', code: 'OUTPUT_NOT_FOUND', message: 'missing' })), /OUTPUT_NOT_FOUND: missing/],
    [result(0, encoded, '', { stdout: { text: encoded, truncated: true } }), /trusted stdout budget/],
  ]
  for (const [shellResult, expected] of scenarios) {
    const h = harness({ results: [shellResult] })
    let caught
    await assert.rejects(
      h.tools[0].execute({ file_path: 'book.ipynb', cell_index: 0 }, { signal: new AbortController().signal }),
      error => {
        caught = error
        return typeof expected === 'function' ? expected(error) : expected.test(String(error))
      },
    )
    assert.equal(String(caught).includes(encoded), false)
  }
})

test('reads the shared mixed-output fixture through the real CLI protocol', async () => {
  const root = mkdtempSync(join(tmpdir(), 'jcli dsh output '))
  const fixture = JSON.parse(readFileSync(new URL('../fixtures/outputs/mixed_outputs.json', import.meta.url), 'utf8'))
  const notebook = join(root, 'mixed.ipynb')
  writeFileSync(notebook, JSON.stringify({
    nbformat: 4,
    nbformat_minor: 5,
    metadata: {},
    cells: [{ cell_type: 'code', id: 'mixed', metadata: {}, execution_count: 7, source: ['1 + 1'], outputs: fixture }],
  }), 'utf8')
  const executable = process.env.UV_PROJECT_ENVIRONMENT
    ? join(process.env.UV_PROJECT_ENVIRONMENT, 'bin', 'j-cli')
    : 'j-cli'
  const h = harness({
    config: { executable },
    shellRun(spec) {
      const child = spawnSync('/bin/bash', ['-c', spec.command], {
        cwd: spec.workdir ?? root,
        encoding: 'utf8',
      })
      return result(child.status, child.stdout, child.stderr, { signal: child.signal })
    },
  })
  const tool = h.tools[0]
  const signal = new AbortController().signal
  const directory = await tool.execute({ file_path: notebook, cell_index: 0 }, { signal })
  assert.equal(directory.outputs.length, 4)
  assert.equal(directory.outputs[1].available_mime_types.includes('image/png'), true)

  const html = await tool.execute({
    file_path: notebook, cell_index: 0, output_index: 1, mime_type: 'text/html',
  }, { signal })
  assert.equal(html.selected.data, '<strong>raw html</strong>')
  assert.equal(tool.output.render({}, html)[0].text, '<strong>raw html</strong>')
})
