import { expect, test } from 'claude-code/testing'

declare const setTimeout: (fn: () => void, ms: number) => unknown

const STATE = '/state'
const USAGE = { input_tokens: 1, output_tokens: 1 }

type Files = Map<string, string>
type Fork = { text?: string; ok?: boolean }

/**
 * Stand-ins for the engine's own file system, session, env, clock and model.
 * Every hook beneath the mod is registered here, before the test first uses `$`;
 * the tests change what they answer through the returned handles.
 */
function world(on: any, initial: Fork = {}) {
  const files: Files = new Map()
  const forks: string[] = []
  const pokes: string[][] = []
  const control = { tool: bashResult() as unknown, fork: initial, failWrites: false }
  files.set(`${STATE}/cli-path`, '/plugin/bin/herdr-caffeinate\n')
  on('tool.call', () => control.tool)
  on('classic.Stop', () => ({}))
  on('classic.SubagentStop', () => ({}))
  on('session.end', (_: unknown, e: { sessionId: string }) => ({ sessionId: e.sessionId }))
  on('env.get', (_: unknown, e: { name: string }) => ({
    value: e.name === 'HERDR_PLUGIN_STATE_DIR' ? STATE : undefined,
  }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('clock.now', () => ({ value: 1_700_000_000_000 }))
  on('fs.write', (_: unknown, e: { path: string; text: string }) => {
    if (control.failWrites) return { deny: 'read-only' }
    files.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.read', (_: unknown, e: { path: string }) => {
    const text = files.get(e.path)
    return text === undefined ? { deny: 'ENOENT' } : { value: text }
  })
  on('fs.list', (_: unknown, e: { path?: string }) => ({
    value: [...files.keys()]
      .filter(k => k.startsWith(`${e.path}/`))
      .map(k => ({ name: k.slice(`${e.path}/`.length), kind: 'file', size: 0, mtimeMs: 0, isLink: false })),
  }))
  on('process.run', (_: unknown, e: { argv: string[] }) => {
    pokes.push([...e.argv])
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  on('model.fork', (_: unknown, e: { prompt: string }) => {
    forks.push(e.prompt)
    return {
      value:
        control.fork.ok === false
          ? { isAnswered: false, reason: 'nothing-to-fork' }
          : {
              isAnswered: true,
              text: control.fork.text ?? '{"class":"persistent","keep_awake":false,"reason":"dev server"}',
              usage: USAGE,
            },
    }
  })
  const task = (id: string, session = 'sess-1') => {
    const text = files.get(`${STATE}/tasks/${session}__${id}.json`)
    return text === undefined ? undefined : JSON.parse(text)
  }
  return { files, forks, pokes, task, control }
}

/** The fork runs after the tool call returns, so let it finish. */
const settle = () => new Promise<void>(resolve => setTimeout(() => resolve(), 20))

const bashResult = (id?: string) => ({
  result: { stdout: '', stderr: '', interrupted: false, ...(id ? { backgroundTaskId: id } : {}) },
})

test('a background build is registered as finite work by its pattern', async ($, on) => {
  const w = world(on)
  w.control.tool = bashResult('bk1')

  await $.tool.call({ tool: 'Bash', command: 'npm run build', description: 'Build the app', run_in_background: true })

  const task = w.task('bk1')
  expect(task.class).toBe('finite')
  expect(task.label).toBe('build')
  expect(task.source).toBe('rule')
  expect(task.kind).toBe('shell')
  expect(task.status).toBe('running')
  expect(task.session_id).toBe('sess-1')
  expect(task.started_at).toBe(1_700_000_000)
  expect(w.pokes[0]).toEqual(['python3', '/plugin/bin/herdr-caffeinate', 'poke'])
  expect(w.forks.length).toBe(0)
})

test('a dev server is registered as persistent and not asked about', async ($, on) => {
  const w = world(on)
  w.control.tool = bashResult('bk2')

  await $.tool.call({ tool: 'Bash', command: 'npm run dev', run_in_background: true })

  expect(w.task('bk2').class).toBe('persistent')
  expect(w.task('bk2').keep_awake).toBe(false)
  expect(w.forks.length).toBe(0)
})

test('a foreground command and a failed launch register nothing', async ($, on) => {
  const w = world(on)
  await $.tool.call({ tool: 'Bash', command: 'npm run build' })

  expect([...w.files.keys()].filter(k => k.includes('/tasks/'))).toEqual([])
})

test('an unknown command is put to the session, and its answer is kept', async ($, on) => {
  const w = world(on)
  w.control.tool = bashResult('bk3')

  await $.tool.call({ tool: 'Bash', command: './mystery.sh --go', description: 'Run it', run_in_background: true })
  await settle()

  expect(w.forks.length).toBe(1)
  expect(w.forks[0]).toContain('./mystery.sh --go')
  expect(w.task('bk3').class).toBe('persistent')
  expect(w.task('bk3').source).toBe('fork')
  expect(w.task('bk3').keep_awake).toBe(false)
})

test('a repeated unknown command is asked about once per session', async ($, on) => {
  const w = world(on, { text: 'sure: {"class":"finite","keep_awake":true,"reason":"codegen"} ok' })
  w.control.tool = bashResult('bkA')
  await $.tool.call({ tool: 'Bash', command: './gen.sh', run_in_background: true })
  await settle()
  w.control.tool = bashResult('bkB')
  await $.tool.call({ tool: 'Bash', command: '  ./gen.sh ', run_in_background: true })
  await settle()

  expect(w.forks.length).toBe(1)
  expect(w.task('bkB').class).toBe('finite')
  expect(w.task('bkB').source).toBe('fork')
})

test('an unanswerable question leaves the task unknown for the daemon to time out', async ($, on) => {
  const w = world(on, { ok: false })
  w.control.tool = bashResult('bk4')

  await $.tool.call({ tool: 'Bash', command: './mystery.sh', run_in_background: true })
  await settle()

  expect(w.task('bk4').class).toBe('unknown')
})

test('a garbled answer is not believed', async ($, on) => {
  const w = world(on, { text: 'I would say finite, probably' })
  w.control.tool = bashResult('bk5')

  await $.tool.call({ tool: 'Bash', command: './mystery.sh', run_in_background: true })
  await settle()

  expect(w.task('bk5').class).toBe('unknown')
})

test('a background subagent is registered as finite work', async ($, on) => {
  const w = world(on)
  w.control.tool = {
    result: { status: 'async_launched', agentId: 'agent-9', description: 'Review', prompt: 'p', outputFile: '/x' },
  }

  await $.tool.call({ tool: 'Agent', description: 'Review the change', prompt: 'Review it' })

  const task = w.task('agent-9')
  expect(task.kind).toBe('subagent')
  expect(task.class).toBe('finite')
  expect(task.keep_awake).toBe(true)
})

test('a task-notification completes the task', async ($, on) => {
  const w = world(on)
  w.control.tool = bashResult('bk6')
  await $.tool.call({ tool: 'Bash', command: 'npm test', run_in_background: true })
  expect(w.task('bk6').status).toBe('running')

  await $.session.append({
    message: {
      type: 'user',
      content: [{ type: 'text', text: '<task-notification>\n<task-id>bk6</task-id>\n<status>failed</status>\n</task-notification>' }],
    },
    door: 'queue',
    origin: { kind: 'task-notification' },
    uuid: 'u1',
  })

  expect(w.task('bk6').status).toBe('failed')
})

test('a notification for another task or session leaves ours alone', async ($, on) => {
  const w = world(on)
  w.control.tool = bashResult('bk7')
  await $.tool.call({ tool: 'Bash', command: 'npm test', run_in_background: true })

  await $.session.append({
    message: { type: 'user', content: [{ type: 'text', text: '<task-notification><task-id>other</task-id><status>completed</status></task-notification>' }] },
    door: 'queue',
    origin: { kind: 'task-notification' },
    uuid: 'u2',
  })
  await $.session.append({
    message: { type: 'user', content: [{ type: 'text', text: 'hello <task-id>bk7</task-id>' }] },
    door: 'prompt',
    origin: { kind: 'composer' },
    uuid: 'u3',
  })

  expect(w.task('bk7').status).toBe('running')
})

test('Stop writes the in-flight snapshot', async ($, on) => {
  const w = world(on)

  await $.classic.Stop({
    stop_hook_active: false,
    session_id: 'sess-1',
    background_tasks: [
      { id: 'bk1', type: 'shell', status: 'running', description: 'x' },
      { id: 'bk2', type: 'shell', status: 'completed', description: 'y' },
    ],
  })

  expect(JSON.parse(w.files.get(`${STATE}/snapshots/sess-1.json`)!)).toEqual({
    session_id: 'sess-1',
    ts: 1_700_000_000,
    task_ids: ['bk1'],
  })
})

test('a question that could not be asked mid-turn is asked at Stop', async ($, on) => {
  const w = world(on, { ok: false })
  w.control.tool = bashResult('bk8')
  await $.tool.call({ tool: 'Bash', command: './later.sh', run_in_background: true })
  await settle()
  expect(w.task('bk8').class).toBe('unknown')

  w.control.fork = { text: '{"class":"finite","keep_awake":true,"reason":"job"}' }
  await $.classic.Stop({ stop_hook_active: false, session_id: 'sess-1', background_tasks: [] })
  await settle()

  expect(w.task('bk8').class).toBe('finite')
})

test('session end completes what the session still had running', async ($, on) => {
  const w = world(on)
  w.control.tool = bashResult('bk9')
  await $.tool.call({ tool: 'Bash', command: 'npm test', run_in_background: true })

  await $.session.end({ reason: 'prompt_input_exit', sessionId: 'sess-1', resume: { sessionId: 'sess-1' } } as never)

  expect(w.task('bk9').status).toBe('completed')
})

test('a failing file system never breaks the tool call', async ($, on) => {
  const w = world(on)
  w.control.failWrites = true
  w.control.tool = bashResult('bk10')

  const ran = await $.tool.call({ tool: 'Bash', command: 'npm test', run_in_background: true })

  expect(ran.deny).toBeUndefined()
})
