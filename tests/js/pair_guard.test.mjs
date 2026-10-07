import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { needsPairGuardPayload } from '../../jupyter_jcli/pair_guard.js'

const cases = JSON.parse(readFileSync(new URL('../fixtures/pair_guard.json', import.meta.url), 'utf8'))
test('shared pair prefilter fixtures and independent pre/post checks', () => {
  const root = mkdtempSync(join(tmpdir(), 'jcli-prefilter-'))
  try {
    mkdirSync(join(root, 'nested'))
    for (const file of ['pair.ipynb', 'nested/pair.ipynb', 'blocked']) writeFileSync(join(root, file), '')
    symlinkSync('loop.ipynb', join(root, 'loop.ipynb'))
    for (const item of cases) {
      const payload = JSON.parse(JSON.stringify(item.payload).replaceAll('$ROOT', root))
      for (const phase of ['pre', 'post']) assert.equal(needsPairGuardPayload(payload, phase), item[phase], `${item.name}: ${phase}`)
    }
    const payload = { tool_name: 'write', tool_input: { file_path: 'created.py' }, cwd: root }
    assert.equal(needsPairGuardPayload(payload, 'pre'), false)
    writeFileSync(join(root, 'created.ipynb'), '')
    assert.equal(needsPairGuardPayload(payload, 'post'), true)
    rmSync(join(root, 'created.ipynb'))
    assert.equal(needsPairGuardPayload(payload, 'pre'), false)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})
