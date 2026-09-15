import assert from 'node:assert/strict'
import { chmodSync, mkdtempSync, writeFileSync } from 'node:fs'
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

function harness({ results = [], shellMode, policy, config, shellRun, get } = {}) {
  const listeners = new Map()
  const calls = []
  const logs = []
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
    logger: { warn(message) { logs.push(String(message)) } },
    get(key) {
      if (get) return get(key)
      return key === 'sandboxPolicy' ? policy : undefined
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
