import { expect, mock, test } from 'claude-code/testing'

declare const setTimeout: (fn: () => void, ms: number) => unknown

const STATE = '/state'
const USAGE = { input_tokens: 1, output_tokens: 1 }
const T0 = 1_700_000_000_000
const SEC = 1000
const HOUR = 3600 * SEC

type Fork = { text?: string; ok?: boolean }

const bashResult = (id?: string) => ({
  result: { stdout: '', stderr: '', interrupted: false, ...(id ? { backgroundTaskId: id } : {}) },
})

/**
 * Stand-ins for everything beneath the mod: tools, the session, files, the
 * process host and the model. Every hook is registered here, before the test
 * first uses `$`; tests steer them through the returned handles.
 */
function world(on: any, initial: Fork = {}, env: Record<string, string> = { CLAUDE_PID: '4242' }) {
  const clock = mock.clock(on, { now: T0 })
  const files = new Map<string, string>()
  const forks: string[] = []
  const spawns: string[][] = []
  const runs: string[][] = []
  const toasts: string[] = []
  const status: Array<string | undefined> = []
  const control = { tool: bashResult() as unknown, fork: initial, active: 0, failSpawn: false }
  files.set(`${STATE}/cli-path`, '/plugin/bin/herdr-caffeinate\n')
  on('tool.call', () => control.tool)
  on('classic.Stop', () => ({}))
  on('classic.SubagentStop', () => ({}))
  on('session.end', (_: unknown, e: { sessionId: string }) => ({ sessionId: e.sessionId }))
  on('session.start', (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('command.register', (_: unknown, e: { name: string }) => ({ value: { command: e.name } }))
  on('env.get', (_: unknown, e: { name: string }) => ({ value: e.name === 'HERDR_PLUGIN_STATE_DIR' ? STATE : undefined }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('ui.toast', (_: unknown, e: { text: string }) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', (_: unknown, e: { text?: string }) => {
    status.push(e.text)
    return { value: undefined }
  })
  on('fs.write', (_: unknown, e: { path: string; text: string }) => {
    files.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.read', (_: unknown, e: { path: string }) => {
    const text = files.get(e.path)
    return text === undefined ? { deny: 'ENOENT' } : { value: text }
  })
  on('process.hookEnv', () => ({ value: env }))
  on('process.run', (_: unknown, e: { argv: string[] }) => {
    runs.push([...e.argv])
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  // A child that lives until the mod leaves its loop.
  on('process.spawn', async function* (_: unknown, e: { argv: string[] }) {
    if (control.failSpawn) throw new Error('no caffeinate')
    spawns.push([...e.argv])
    control.active += 1
    try {
      for (;;) {
        await new Promise<void>(resolve => setTimeout(() => resolve(), 15))
        yield { stream: 'stdout', text: '.' }
      }
    } finally {
      control.active -= 1
    }
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
  return { clock, files, forks, spawns, runs, toasts, status, control }
}

const settle = () => new Promise<void>(resolve => setTimeout(() => resolve(), 60))

async function launch($: any, w: ReturnType<typeof world>, id: string, command: string, description?: string) {
  w.control.tool = bashResult(id)
  await $.tool.call({ tool: 'Bash', command, description, run_in_background: true })
  await settle()
}

const awake = async ($: any, args = '') =>
  (await $.command.run({ command: 'awake', args, origin: { kind: 'composer' }, presentation: { layout: 'main', columns: 100 } })).text as string

const stop = ($: any, background_tasks: Array<{ id: string; status: string }>) =>
  $.classic.Stop({ stop_hook_active: false, session_id: 'sess-1', background_tasks })

const notify = ($: any, id: string, status = 'completed') =>
  $.session.append({
    message: { type: 'user', content: [{ type: 'text', text: `<task-notification>\n<task-id>${id}</task-id>\n<status>${status}</status>\n</task-notification>` }] },
    door: 'queue',
    origin: { kind: 'task-notification' },
    uuid: `u-${id}`,
  })

const end = async ($: any) => {
  await $.session.end({ reason: 'prompt_input_exit', sessionId: 'sess-1', resume: { sessionId: 'sess-1' } })
  await settle()
}

test('a background build holds a sleep assertion tied to the Claude process', async ($, on) => {
  const w = world(on)
  await launch($, w, 'bk1', 'npm run build', 'Production build')

  expect(w.spawns).toEqual([['caffeinate', '-i', '-w', '4242']])
  expect(w.control.active).toBe(1)
  const text = await awake($)
  expect(text).toContain('Holding the Mac awake')
  expect(text).toContain('bk1')
  expect(text).toContain('finite/build')
  await end($)
  expect(w.control.active).toBe(0)
})

test('a dev server never holds', async ($, on) => {
  const w = world(on)
  await launch($, w, 'dev', 'npm run dev', 'Dev server')

  expect(w.spawns).toEqual([])
  expect(await awake($)).toContain('persistent service')
  expect(await awake($)).toContain('Not holding')
})

test('a foreground command registers nothing', async ($, on) => {
  const w = world(on)
  await $.tool.call({ tool: 'Bash', command: 'npm run build' })
  await settle()

  expect(w.spawns).toEqual([])
  expect(await awake($)).toContain('No background tasks')
})

test('several tasks share one assertion', async ($, on) => {
  const w = world(on)
  await launch($, w, 'a', 'npm run build')
  await launch($, w, 'b', 'pytest')
  await launch($, w, 'c', 'cargo test')

  expect(w.spawns.length).toBe(1)
  expect(w.control.active).toBe(1)
  await notify($, 'a')
  await notify($, 'b')
  await settle()
  expect(w.control.active).toBe(1)
  await notify($, 'c')
  await settle()
  expect(w.control.active).toBe(0)
})

test('a task-notification releases the assertion; a stranger does not', async ($, on) => {
  const w = world(on)
  await launch($, w, 'bk2', 'npm test')
  await notify($, 'other')
  await settle()
  expect(w.control.active).toBe(1)

  await notify($, 'bk2', 'failed')
  await settle()
  expect(w.control.active).toBe(0)
  expect(await awake($)).toContain('No background tasks')
})

test('a Stop snapshot that no longer lists a task ends it, once it is old enough', async ($, on) => {
  const w = world(on)
  await launch($, w, 'bk3', 'npm run build')

  await stop($, [])
  await settle()
  expect(w.control.active).toBe(1)

  await w.clock.advance(6 * SEC)
  await stop($, [{ id: 'bk3', status: 'running' }])
  await settle()
  expect(w.control.active).toBe(1)

  await w.clock.advance(6 * SEC)
  await stop($, [])
  await settle()
  expect(w.control.active).toBe(0)
})

test('a background subagent counts as finite work', async ($, on) => {
  const w = world(on)
  w.control.tool = { result: { status: 'async_launched', agentId: 'agent-9', description: 'Review', prompt: 'p', outputFile: '/x' } }
  await $.tool.call({ tool: 'Agent', description: 'Review the change', prompt: 'Review it' })
  await settle()

  expect(w.control.active).toBe(1)
  expect(await awake($)).toContain('subagent')
  await notify($, 'agent-9')
  await settle()
  expect(w.control.active).toBe(0)
})

test('an unknown command is put to the session; a persistent answer lets go', async ($, on) => {
  const w = world(on)
  await launch($, w, 'bk4', './mystery.sh --go', 'Run it')

  expect(w.forks.length).toBe(1)
  expect(w.forks[0]).toContain('./mystery.sh --go')
  expect(w.control.active).toBe(0)
  expect(await awake($)).toContain('persistent/dev server (fork)')
})

test('a finite answer holds, and a repeated command is not asked twice', async ($, on) => {
  const w = world(on, { text: 'sure: {"class":"finite","keep_awake":true,"reason":"codegen"} ok' })
  await launch($, w, 'a', './gen.sh')
  await launch($, w, 'b', '  ./gen.sh ')

  expect(w.forks.length).toBe(1)
  expect(w.control.active).toBe(1)
  expect(await awake($)).toContain('finite/codegen (fork)')
  await end($)
})

test('with no answer, an unknown task holds for its grace, then falls back to ignore', async ($, on) => {
  const w = world(on, { ok: false })
  await launch($, w, 'bk5', './mystery.sh')

  expect(w.control.active).toBe(1)
  expect(await awake($)).toContain('counted for')
  await w.clock.advance(130 * SEC)
  await settle()
  expect(w.control.active).toBe(0)
  expect(await awake($)).toContain('fallback is ignore')
})

test('the fallback can be keep', { options: { unknownFallback: 'keep' } }, async ($, on) => {
  const w = world(on, { ok: false })
  await launch($, w, 'bk6', './mystery.sh')
  await w.clock.advance(130 * SEC)
  await settle()
  expect(w.control.active).toBe(1)
  expect(await awake($)).toContain('fallback is keep awake')
  await end($)
})

test('a garbled answer is not believed', async ($, on) => {
  const w = world(on, { text: 'I would say finite, probably' })
  await launch($, w, 'bk7', './mystery.sh')

  expect(await awake($)).toContain('unknown')
  await end($)
})

test('a question that could not be asked mid-turn is asked at Stop', async ($, on) => {
  const w = world(on, { ok: false })
  await launch($, w, 'bk8', './later.sh')
  w.control.fork = { text: '{"class":"persistent","keep_awake":false,"reason":"daemon"}' }
  await stop($, [{ id: 'bk8', status: 'running' }])
  await settle()

  expect(w.control.active).toBe(0)
  expect(await awake($)).toContain('persistent')
})

test('without CLAUDE_PID a five minute assertion is renewed, never doubled', async ($, on) => {
  const w = world(on, {}, {})
  await launch($, w, 'bk9', 'npm run build')
  expect(w.spawns).toEqual([['caffeinate', '-i', '-t', '300']])

  await w.clock.advance(250 * SEC)
  await settle()
  expect(w.spawns.length).toBe(2)
  expect(w.control.active).toBe(1)
  await end($)
  expect(w.control.active).toBe(0)
})

test('session end releases everything', async ($, on) => {
  const w = world(on)
  await launch($, w, 'a', 'npm run build')
  await launch($, w, 'b', 'npm test')
  await end($)

  expect(w.control.active).toBe(0)
  expect(await awake($)).toContain('No background tasks')
})

test('/awake keep, ignore and clear override a task', async ($, on) => {
  const w = world(on)
  await launch($, w, 'dev1', 'npm run dev')
  await launch($, w, 'bld1', 'npm run build')
  expect(w.control.active).toBe(1)

  expect(await awake($, 'ignore bld1')).toContain('ignored by your override')
  await settle()
  expect(w.control.active).toBe(0)

  expect(await awake($, 'keep dev')).toContain('kept awake by your override')
  await settle()
  expect(w.control.active).toBe(1)

  await awake($, 'clear dev1')
  await settle()
  expect(w.control.active).toBe(0)
  expect(await awake($, 'keep nope')).toContain('No task matches')
  expect(await awake($, 'sing')).toContain('Usage')
})

test('a very long task raises one toast and keeps holding', { options: { stuckAfterHours: 1 }, timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await launch($, w, 'long', 'npm run build', 'Overnight build')

  await w.clock.advance(HOUR + 60 * SEC)
  await settle()
  await w.clock.advance(60 * SEC)

  expect(w.toasts.length).toBe(1)
  expect(w.toasts[0]).toContain('Overnight build')
  expect(w.toasts[0]).toContain('/awake ignore long')
  expect(w.control.active).toBe(1)
  expect(await awake($)).toContain('possibly stuck')
  await end($)
})

test('the hard cap, when set, stops holding unless the task is kept', { options: { hardCapHours: 1 }, timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await launch($, w, 'cap', 'npm run build')
  await launch($, w, 'kept', 'npm test')
  await awake($, 'keep kept')
  await w.clock.advance(HOUR + 120 * SEC)
  await settle()
  const text = await awake($)
  expect(text).not.toContain('cap  ')
  expect(text).toContain('kept')
  expect(w.control.active).toBe(1)
  await end($)
})

test('the registry for the herdr plugin is written only when asked for', async ($, on) => {
  const w = world(on)
  await launch($, w, 'quiet', 'npm run build')
  await stop($, [{ id: 'quiet', status: 'running' }])

  expect([...w.files.keys()].filter(k => k !== `${STATE}/cli-path`)).toEqual([])
  expect(w.runs).toEqual([])
  await end($)
})

test('with herdrRegistry on the task and the snapshot are written and the daemon poked', { options: { herdrRegistry: true } }, async ($, on) => {
  const w = world(on)
  await launch($, w, 'mirrored', 'npm run build')
  await stop($, [{ id: 'mirrored', status: 'running' }])

  expect(JSON.parse(w.files.get(`${STATE}/tasks/sess-1__mirrored.json`)!).class).toBe('finite')
  expect(JSON.parse(w.files.get(`${STATE}/snapshots/sess-1.json`)!).task_ids).toEqual(['mirrored'])
  expect(w.runs[0]).toEqual(['python3', '/plugin/bin/herdr-caffeinate', 'poke'])
  await notify($, 'mirrored')
  await settle()
  expect(JSON.parse(w.files.get(`${STATE}/tasks/sess-1__mirrored.json`)!).status).toBe('completed')
})

test('the status line shows the count while holding', { options: { showStatus: true } }, async ($, on) => {
  const w = world(on)
  await launch($, w, 'a', 'npm run build')
  expect(w.status.at(-1)).toBe('☕ awake · 1 background task')
  await notify($, 'a')
  await settle()
  expect(w.status.at(-1)).toBeUndefined()
})

test('a child that cannot start never breaks the tool call', async ($, on) => {
  const w = world(on)
  w.control.failSpawn = true
  w.control.tool = bashResult('x')
  const ran = await $.tool.call({ tool: 'Bash', command: 'npm test', run_in_background: true })

  expect(ran.deny).toBeUndefined()
})
