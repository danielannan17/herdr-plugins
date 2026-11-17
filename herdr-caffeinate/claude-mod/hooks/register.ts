import type { EngineInterface, Register } from 'claude-code'
import * as core from './core'
import { DEFAULT_RULES } from './rules'

// Registers the background work this session starts with the herdr-caffeinate
// plugin, which keeps the Mac awake while finite work runs. Nothing here may
// block or alter a tool call, so every handler does its own work after
// `next(e)` and swallows its own errors.

type Engine = EngineInterface
type Dirs = { state: string; config: string }

const ALL_PLUGIN_DEFAULT_RULES = DEFAULT_RULES

// Answers from the session's own model, by (session, command): a repeated
// command is never asked about twice.
const asked = new Map<string, Promise<core.ForkAnswer | null>>()
// Unknown tasks whose question could not be asked yet (no turn had finished).
const waiting: Array<{ session: string; taskId: string; command: string; description: string }> = []
const lastSnapshot = new Map<string, string>()

async function nowSec($: Engine): Promise<number> {
  return (await $.clock.now()) / 1000
}

async function dirs($: Engine): Promise<Dirs> {
  const home = await $.env.get('HOME')
  const xdg = await $.env.get('XDG_STATE_HOME')
  const herdrState = await $.env.get('HERDR_PLUGIN_STATE_DIR')
  const herdrConfig = await $.env.get('HERDR_PLUGIN_CONFIG_DIR')
  return {
    state: core.stateDir({ herdr: herdrState, xdg, home }),
    config: core.configDir({ herdr: herdrConfig, home }),
  }
}

async function readJson<T>($: Engine, path: string): Promise<T | null> {
  try {
    return JSON.parse(String(await $.fs.read(path))) as T
  } catch {
    return null
  }
}

async function rules($: Engine, d: Dirs): Promise<core.Rules> {
  const user = await readJson<Partial<core.Rules>>($, `${d.config}/rules.json`)
  return core.mergeRules(ALL_PLUGIN_DEFAULT_RULES, user)
}

/** Ask the daemon to look again now. Best effort: the daemon also re-checks on its own tick. */
async function poke($: Engine, d: Dirs): Promise<void> {
  try {
    const cli = String(await $.fs.read(`${d.state}/cli-path`)).trim()
    if (cli) await $.process.run(['python3', cli, 'poke'], { timeoutMs: 5000 })
  } catch {
    // plugin not installed, or herdr not running
  }
}

async function save($: Engine, d: Dirs, task: core.Task): Promise<void> {
  await $.fs.write(core.taskPath(d.state, task.session_id, task.task_id), JSON.stringify(task, null, 1) + '\n')
}

async function load($: Engine, d: Dirs, session: string, taskId: string): Promise<core.Task | null> {
  return readJson<core.Task>($, core.taskPath(d.state, session, taskId))
}

async function askSession($: Engine, session: string, command: string, description: string) {
  const key = `${session}\u0000${core.normalize(command)}`
  const known = asked.get(key)
  if (known) return known
  const pending = (async (): Promise<core.ForkAnswer | null> => {
    const reply = await $.model.fork({ prompt: core.forkPrompt(command, description), effort: 'low' })
    return reply.isAnswered ? core.parseForkAnswer(reply.text) : null
  })().catch(() => null)
  asked.set(key, pending)
  // A failed or unanswerable question may be asked again later.
  void pending.then(answer => {
    if (!answer) asked.delete(key)
  })
  return pending
}

/** Settle an unknown task by asking the agent that started it. Never awaited by a hook. */
async function resolveUnknown($: Engine, d: Dirs, session: string, taskId: string, command: string, description: string) {
  const answer = await askSession($, session, command, description)
  if (!answer) {
    waiting.push({ session, taskId, command, description })
    return
  }
  const task = await load($, d, session, taskId)
  if (!task || task.class !== 'unknown') return
  const changed = core.reclassify(
    task,
    { cls: answer.cls, keepAwake: answer.keepAwake, source: 'fork', label: answer.reason || null },
    await nowSec($),
  )
  await save($, d, changed)
  await poke($, d)
}

async function track($: Engine, e: { command: string; description?: string }, taskId: string) {
  const d = await dirs($)
  const session = await $.session.id()
  const command = e.command
  const description = e.description ?? ''
  const [cls, label] = core.classify(command, description, await rules($, d))
  const existing = await load($, d, session, taskId)
  if (existing) return
  const task = core.newTask({
    taskId, sessionId: session, kind: 'shell', command, description, cls,
    keepAwake: cls !== 'persistent', source: 'rule', label, now: await nowSec($),
  })
  await save($, d, task)
  await poke($, d)
  if (cls === 'unknown') void resolveUnknown($, d, session, taskId, command, description).catch(() => undefined)
}

// Each Stop carries the session's in-flight background tasks: the daemon
// treats a registered task missing from the latest snapshot as finished.
async function snapshot($: Engine, e: { session_id: string; background_tasks?: Array<{ id: string; status: string }> }): Promise<void> {
  const d = await dirs($)
  const ids = core.inFlightIds(e.background_tasks)
  const signature = ids.slice().sort().join(',')
  await $.fs.write(
    core.snapshotPath(d.state, e.session_id),
    JSON.stringify({ session_id: e.session_id, ts: await nowSec($), task_ids: ids.slice().sort() }) + '\n',
  )
  if (lastSnapshot.get(e.session_id) !== signature) {
    lastSnapshot.set(e.session_id, signature)
    await poke($, d)
  }
  // Questions that could not be asked mid-turn are asked now that a turn has ended.
  for (const item of waiting.splice(0)) {
    void resolveUnknown($, d, item.session, item.taskId, item.command, item.description).catch(() => undefined)
  }
}

async function showStatus($: Engine, d: Dirs): Promise<void> {
  const text = String(await $.fs.read(`${d.state}/status.txt`).catch(() => '')).trim()
  $.ui.status(text || undefined)
}

export const register: Register = (on, options) => {
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const ran = await next(e)
    try {
      // The launch result names the task; a command backgrounded by timeout or
      // by the user carries the id too, so the result decides, not the flag.
      const id = ran.deny === undefined && ran.isError !== true ? ran.result?.backgroundTaskId : undefined
      if (id) await track($, { command: e.command, description: e.description }, id)
    } catch {
      // degrade quietly
    }
    return ran
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'Agent' }, async ($, e, next) => {
    const ran = await next(e)
    try {
      const result = (ran.deny === undefined && ran.isError !== true ? ran.result : undefined) as
        | { status?: string; agentId?: string }
        | undefined
      if (result?.status === 'async_launched' && result.agentId) {
        const d = await dirs($)
        const session = await $.session.id()
        if (!(await load($, d, session, result.agentId))) {
          await save($, d, core.newTask({
            taskId: result.agentId, sessionId: session, kind: 'subagent', description: e.description,
            cls: 'finite', keepAwake: true, source: 'rule', label: 'subagent', now: await nowSec($),
          }))
          await poke($, d)
        }
      }
    } catch {
      // degrade quietly
    }
    return ran
  }).catch(($, e, next) => next(e))

  // A finished background task arrives as a task-notification row.
  on('session.append', async ($, e, next) => {
    const stored = await next(e)
    try {
      if (e.origin.kind === 'task-notification') {
        const text = e.message.content.map(b => (b.type === 'text' ? b.text : '')).join('\n')
        const note = core.parseNotification(text)
        if (note) {
          const d = await dirs($)
          const session = await $.session.id()
          const task = await load($, d, session, note.taskId)
          if (task) {
            await save($, d, core.finish(task, note.status, await nowSec($)))
            await poke($, d)
          }
        }
      }
    } catch {
      // degrade quietly
    }
    return stored
  }).catch(($, e, next) => next(e))

  on('classic.Stop', async ($, e, next) => {
    const result = await next(e)
    try {
      await snapshot($, e)
    } catch {
      // degrade quietly
    }
    return result
  }).catch(($, e, next) => next(e))

  on('classic.SubagentStop', async ($, e, next) => {
    const result = await next(e)
    try {
      await snapshot($, e)
    } catch {
      // degrade quietly
    }
    return result
  }).catch(($, e, next) => next(e))

  // A session that ends takes its background work with it.
  on('session.end', async ($, e, next) => {
    const result = await next(e)
    try {
      const d = await dirs($)
      const stem = core.sessionPrefix(e.sessionId)
      for (const entry of await $.fs.list(`${d.state}/tasks`)) {
        if (entry.name.startsWith(stem) && entry.name.endsWith('.json')) {
          const task = await readJson<core.Task>($, `${d.state}/tasks/${entry.name}`)
          if (task && task.status === 'running') await save($, d, core.finish(task, 'completed', await nowSec($)))
        }
      }
      lastSnapshot.delete(e.sessionId)
      await poke($, d)
    } catch {
      // degrade quietly
    }
    return result
  }).catch(($, e, next) => next(e))

  if (options.showStatus === true) {
    on('session.start', async ($, e, next) => {
      const started = await next(e)
      try {
        const d = await dirs($)
        await showStatus($, d)
        $.clock.every(20000, () => void showStatus($, d).catch(() => undefined))
      } catch {
        // degrade quietly
      }
      return started
    })
  }
}
