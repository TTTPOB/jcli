import { afterAll, beforeAll, expect, mock, test } from "bun:test"
import {
  chmodSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"

const schemaValue = () => ({
  describe() { return this },
  int() { return this },
  nonnegative() { return this },
  positive() { return this },
  optional() { return this },
})
const tool = Object.assign((definition) => definition, {
  schema: { string: schemaValue, number: schemaValue },
})
mock.module("@opencode-ai/plugin", () => ({ tool }))

const fakeJcliSource = (calls, response, mode) => `#!/usr/bin/env bun
import { appendFileSync, readFileSync } from "node:fs"

const argv = process.argv.slice(2)
let payload
if (argv[0] === "_hooks") {
  const input = await Bun.stdin.text()
  payload = input ? JSON.parse(input) : undefined
}
appendFileSync(
  ${JSON.stringify(calls)},
  JSON.stringify({ argv, cwd: process.cwd(), payload }) + "\\n",
)

const mode = readFileSync(${JSON.stringify(mode)}, "utf8").trim()
if (argv[0] === "_hooks") {
  process.stdout.write("{}")
} else if (mode === "cancel") {
  await Bun.sleep(30_000)
} else if (mode === "over-limit") {
  process.stdout.write("x".repeat(33 * 1024 * 1024))
} else if (mode === "failure") {
  process.stdout.write("{not-json")
  process.stderr.write("structured reader failed")
  process.exit(7)
} else {
  process.stdout.write(await Bun.file(${JSON.stringify(response)}).text())
}
`

let root
let project
let nested
let callsPath
let responsePath
let modePath
let outputTool
let hooks
let permissions

const source = (outputIndex = undefined) => ({
  kind: "notebook",
  path: path.join(project, "analysis.ipynb"),
  cell_index: 2,
  mapping: "direct",
  ...(outputIndex === undefined ? {} : { output_index: outputIndex }),
})

const setMode = (mode) => writeFileSync(modePath, mode, "utf8")

const writeResponse = (response) => {
  writeFileSync(responsePath, JSON.stringify(response), "utf8")
  setMode("response")
}

const context = (abort = new AbortController()) => ({
  sessionID: "session",
  messageID: "message",
  agent: "test",
  directory: nested,
  worktree: project,
  abort: abort.signal,
  metadata() {},
  async ask(request) {
    permissions.push(request)
  },
})

const calls = () =>
  readFileSync(callsPath, "utf8")
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line))

beforeAll(async () => {
  root = mkdtempSync(path.join(os.tmpdir(), "jcli-opencode-test-"))
  project = path.join(root, "project")
  nested = path.join(project, "nested")
  mkdirSync(nested, { recursive: true })
  callsPath = path.join(root, "calls.jsonl")
  responsePath = path.join(root, "response.json")
  modePath = path.join(root, "mode")
  writeFileSync(callsPath, "", "utf8")
  setMode("response")
  const fakeJcli = path.join(root, "fake-j-cli")
  writeFileSync(fakeJcli, fakeJcliSource(callsPath, responsePath, modePath), "utf8")
  chmodSync(fakeJcli, 0o755)
  process.env.JCLI_BIN = fakeJcli

  const module = await import(pathToFileURL(process.env.JCLI_TEST_PLUGIN).href)
  hooks = await module.JcliPlugin({
    client: { app: { log: async () => {} } },
    directory: project,
  })
  outputTool = hooks.tool.read_notebook_output
})

afterAll(() => rmSync(root, { recursive: true, force: true }))

test("registers a directory/read tool using invocation cwd and read permission", async () => {
  permissions = []
  writeResponse({
    schema_version: 1,
    status: "ok",
    source: source(),
    outputs: [
      { output_index: 0, output_type: "stream", available_mime_types: [], name: "stderr" },
      { output_index: 1, output_type: "display_data", available_mime_types: ["image/png", "text/html"] },
    ],
  })

  const result = await outputTool.execute(
    { file_path: "../analysis.ipynb", cell_index: 2 },
    context(),
  )

  expect(JSON.parse(result.output)).toHaveLength(2)
  expect(result.metadata).toEqual({ schema_version: 1, source: source() })
  expect(permissions).toEqual([
    {
      permission: "read",
      patterns: ["analysis.ipynb"],
      always: ["*"],
      metadata: { path: path.join(project, "analysis.ipynb") },
    },
  ])
  const call = calls().at(-1)
  expect(call.cwd).toBe(nested)
  expect(call.argv).toEqual([
    "--json",
    "notebook",
    "outputs",
    "../analysis.ipynb",
    "--cell",
    "2",
  ])
})

test("projects text, JSON, image, and stream responses into ToolResult", async () => {
  permissions = []
  const fixture = JSON.parse(
    readFileSync(process.env.JCLI_OUTPUT_FIXTURE, "utf8"),
  )
  const html = fixture[1].data["text/html"]
  writeResponse({
    schema_version: 1,
    status: "ok",
    source: source(1),
    output_type: "display_data",
    available_mime_types: Object.keys(fixture[1].data),
    metadata: fixture[1].metadata,
    selected: {
      mime_type: "text/html",
      encoding: "utf-8",
      bytes: Buffer.byteLength(html),
      data: html,
      offset: 0,
      returned_characters: html.length,
      total_characters: html.length,
      truncated: false,
    },
  })
  const htmlResult = await outputTool.execute(
    { file_path: "../analysis.ipynb", cell_index: 2, output_index: 1, mime_type: "text/html" },
    context(),
  )
  expect(htmlResult.output).toBe(html)
  expect(htmlResult.metadata.source).toEqual(source(1))
  expect(htmlResult.metadata.selected.data).toBeUndefined()

  const jsonValue = fixture[2].data["application/json"]
  writeResponse({
    schema_version: 1,
    status: "ok",
    source: source(2),
    output_type: "execute_result",
    available_mime_types: Object.keys(fixture[2].data),
    metadata: fixture[2].metadata,
    execution_count: fixture[2].execution_count,
    selected: {
      mime_type: "application/json",
      encoding: "json",
      bytes: 13,
      data: jsonValue,
    },
  })
  const jsonResult = await outputTool.execute(
    { file_path: "../analysis.ipynb", cell_index: 2, output_index: 2 },
    context(),
  )
  expect(JSON.parse(jsonResult.output)).toEqual(jsonValue)
  expect(jsonResult.metadata.execution_count).toBe(7)

  const png = fixture[1].data["image/png"]
  writeResponse({
    schema_version: 1,
    status: "ok",
    source: source(1),
    output_type: "display_data",
    available_mime_types: Object.keys(fixture[1].data),
    metadata: fixture[1].metadata,
    selected: { mime_type: "image/png", encoding: "base64", bytes: 8, data: png },
  })
  const imageResult = await outputTool.execute(
    { file_path: "../analysis.ipynb", cell_index: 2, output_index: 1 },
    context(),
  )
  expect(imageResult.attachments).toEqual([
    {
      type: "file",
      mime: "image/png",
      url: `data:image/png;base64,${png}`,
      filename: "analysis.ipynb",
    },
  ])
  expect(imageResult.output).not.toContain(png)
  expect(JSON.stringify(imageResult.metadata)).not.toContain(png)

  const streamText = fixture[0].text.join("")
  writeResponse({
    schema_version: 1,
    status: "ok",
    source: source(0),
    output_type: "stream",
    available_mime_types: [],
    stream: {
      name: fixture[0].name,
      bytes: Buffer.byteLength(streamText),
      data: streamText,
      offset: 0,
      returned_characters: streamText.length,
      total_characters: streamText.length,
      truncated: false,
    },
  })
  const streamResult = await outputTool.execute(
    { file_path: "../analysis.ipynb", cell_index: 2, output_index: 0 },
    context(),
  )
  expect(streamResult.output).toBe(streamText)
  expect(streamResult.metadata.stream.data).toBeUndefined()
})

test("reads output larger than ordinary shell capture budgets", async () => {
  permissions = []
  const text = "large-output\n".repeat(20_000)
  writeResponse({
    schema_version: 1,
    status: "ok",
    source: source(0),
    output_type: "stream",
    available_mime_types: [],
    stream: {
      name: "stdout",
      bytes: Buffer.byteLength(text),
      data: text,
      offset: 0,
      returned_characters: text.length,
      total_characters: text.length,
      truncated: false,
    },
  })

  const result = await outputTool.execute(
    { file_path: "../analysis.ipynb", cell_index: 2, output_index: 0 },
    context(),
  )
  expect(result.output).toBe(text)
})

test("cancels the private CLI process through the tool abort signal", async () => {
  permissions = []
  setMode("cancel")
  const controller = new AbortController()
  const pending = outputTool.execute(
    { file_path: "../analysis.ipynb", cell_index: 2, output_index: 0 },
    context(controller),
  )
  setTimeout(() => controller.abort(), 20)
  await expect(pending).rejects.toThrow("cancelled")
})

test("rejects stdout beyond the 32 MiB transport envelope", async () => {
  permissions = []
  setMode("over-limit")
  await expect(
    outputTool.execute(
      { file_path: "../analysis.ipynb", cell_index: 2, output_index: 0 },
      context(),
    ),
  ).rejects.toThrow("j-cli stdout exceeded 33554433 bytes")
})

test("checks nonzero status before parsing stdout", async () => {
  permissions = []
  setMode("failure")
  await expect(
    outputTool.execute(
      { file_path: "../analysis.ipynb", cell_index: 2, output_index: 0 },
      context(),
    ),
  ).rejects.toThrow("structured reader failed")
})

test("keeps existing execution guards registered", async () => {
  permissions = []
  setMode("response")
  await hooks["tool.execute.before"](
    { tool: "bash", sessionID: "session", callID: "call" },
    { args: { command: "python analysis.py", workdir: "nested" } },
  )
  const recent = calls().slice(-2)
  expect(recent.map((call) => call.argv)).toEqual([
    ["_hooks", "notebook-exec-guard"],
    ["_hooks", "python-run-guard"],
  ])
  expect(recent[0].cwd).toBe(nested)
})
