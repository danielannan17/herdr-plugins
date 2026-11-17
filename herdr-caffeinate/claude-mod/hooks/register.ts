import type { EngineInterface, Register } from 'claude-code'
import * as core from './core'
import { DEFAULT_RULES } from './rules'

// Keeps the Mac from idle-sleeping while THIS session has background work that
// matters: a `caffeinate -i` child exists exactly while at least one live task
// counts. Nothing here may block or alter a tool call, so every handler does
// its own work after `next(e)` and swallows its own errors.

type Engine = EngineInterface
type Dirs = { state: string; config: string }
type Holder = { stream: { return: (v: never) => Promise<unknown> }; timed: boolean; since: number; pid: string }

const TICK_MS = 10_000
const TIMED_REFRESH_SEC = 240

// Per-process state. A reload starts it over, and the unloaded module's child dies with it.
const tasks = new Map<string, core.Task>()
const asked = new Map<string, Promise<core.ForkAnswer | null>>()
const waiting: Array<{ taskId: string; command: string; description: string }> = []
const lastSnapshot = new Map<string, string>()
let policy: core.Policy = core.DEFAULT_POLICY
let showStatus = false
let mirrorToHerdr = false
let holder: Holder | null = null
let tick: { cancel: () => unknown } | null = null
let queue: Promise<unknown> = Promise.resolve()

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
  return core.mergeRules(DEFAULT_RULES, user)
}

// --- the sleep assertion ---------------------------------------------------

async function startHolder($: Engine, nowMs: number): Promise<void> {
  if (holder) return
  // CLAUDE_PID is the Claude Code process: `-w` ties the assertion to it, so the
  // kernel drops it however the session ends. Without it, a five minute
  // assertion renewed while work continues expires on its own.
  const env = await $.process.hookEnv().catch(() => ({}) as Record<string, string>)
  const pid = /^\d+$/.test(env.CLAUDE_PID ?? '') ? (env.CLAUDE_PID as string) : ''
  const stream = $.process.spawn({ argv: pid ? ['caffeinate', '-i', '-w', pid] : ['caffeinate', '-i', '-t', '300'] })
  const mine: Holder = { stream, timed: pid === '', since: nowMs, pid }
  holder = mine
  void (async () => {
    try {
      for await (const piece of stream) void piece
    } catch {
      // the child failed to start or ended
    }
    if (holder === mine) holder = null
  })()
}

async function stopHolder(): Promise<void> {
  const mine = holder
  holder = null
  if (mine) await mine.stream.return(undefined as never).catch(() => undefined)
}

// --- evaluation ---------------------------------------------------------------

function evaluate($: Engine): Promise<unknown> {
  queue = queue.then(() => evaluateNow($)).catch(() => undefined)
  return queue
}

async function evaluateNow($: Engine): Promise<void> {
  const now = await nowSec($)
  const nowMs = now * 1000
  for (const task of [...tasks.values()]) {
    if (core.overHardCap(task, now, policy)) {
      tasks.delete(task.task_id)
      await mirror($, core.finish(task, 'completed', now))
    }
  }
  let counting = 0
  for (const task of tasks.values()) {
    const v = core.verdict(task, now, policy)
    if (v.counts) counting += 1
    if (v.stuck && !task.stuck_notified_at) {
      task.stuck_notified_at = now
      $.ui.toast(`Still keeping the Mac awake after ${core.ageText(now - task.started_at)}: ${(task.description || task.command || task.task_id).slice(0, 60)}. /awake ignore ${task.task_id} to stop.`, { timeoutMs: 15000 })
    }
  }
  if (counting > 0) {
    if (holder?.timed && nowMs - holder.since > TIMED_REFRESH_SEC * 1000) await stopHolder()
    await startHolder($, nowMs)
  } else {
    await stopHolder()
  }
  if (tasks.size > 0 && !tick) tick = $.clock.every(TICK_MS, () => void evaluate($))
  if (tasks.size === 0 && tick) {
    tick.cancel()
    tick = null
  }
  $.ui.status(showStatus && counting > 0 ? `☕ awake · ${counting} background task${counting === 1 ? '' : 's'}` : undefined)
}

// --- the optional registry the herdr plugin reads -------------------------------

async function poke($: Engine, d: Dirs): Promise<void> {
  try {
    const cli = String(await $.fs.read(`${d.state}/cli-path`)).trim()
    if (cli) await $.process.run(['python3', cli, 'poke'], { timeoutMs: 5000 })
  } catch {
    // plugin not installed, or herdr not running
  }
}

async function mirror($: Engine, task: core.Task): Promise<void> {
  if (!mirrorToHerdr) return
  try {
    const d = await dirs($)
    await $.fs.write(core.taskPath(d.state, task.session_id, task.task_id), JSON.stringify(task, null, 1) + '\n')
    await poke($, d)
  } catch {
    // degrade quietly
  }
}

async function mirrorSnapshot($: Engine, sessionId: string, ids: string[]): Promise<void> {
  if (!mirrorToHerdr) return
  try {
    const d = await dirs($)
    const sorted = ids.slice().sort()
    await $.fs.write(
      core.snapshotPath(d.state, sessionId),
      JSON.stringify({ session_id: sessionId, ts: await nowSec($), task_ids: sorted }) + '\n',
    )
    if (lastSnapshot.get(sessionId) !== sorted.join(',')) {
      lastSnapshot.set(sessionId, sorted.join(','))
      await poke($, d)
    }
  } catch {
    // degrade quietly
  }
}

// --- classification ---------------------------------------------------------------

async function askSession($: Engine, session: string, command: string, description: string) {
  const key = `${session}\u0000${core.normalize(command)}`
  const known = asked.get(key)
  if (known) return known
  const pending = (async (): Promise<core.ForkAnswer | null> => {
    const reply = await $.model.fork({ prompt: core.forkPrompt(command, description), effort: 'low' })
    return reply.isAnswered ? core.parseForkAnswer(reply.text) : null
  })().catch(() => null)
  asked.set(key, pending)
  void pending.then(answer => {
    if (!answer) asked.delete(key)
  })
  return pending
}

/** Settle an unknown task by asking the agent that started it. Never awaited by a hook. */
async function resolveUnknown($: Engine, taskId: string, command: string, description: string): Promise<void> {
  const answer = await askSession($, await $.session.id(), command, description)
  if (!answer) {
    waiting.push({ taskId, command, description })
    return
  }
  const task = tasks.get(taskId)
  if (!task || task.class !== 'unknown') return
  const changed = core.reclassify(
    task,
    { cls: answer.cls, keepAwake: answer.keepAwake, source: 'fork', label: answer.reason || null },
    await nowSec($),
  )
  tasks.set(taskId, changed)
  await mirror($, changed)
  await evaluate($)
}

async function track($: Engine, e: { command: string; description?: string }, taskId: string): Promise<void> {
  if (tasks.has(taskId)) return
  const d = await dirs($)
  const session = await $.session.id()
  const [cls, label] = core.classify(e.command, e.description ?? '', await rules($, d))
  const task = core.newTask({
    taskId, sessionId: session, kind: 'shell', command: e.command, description: e.description ?? '', cls,
    keepAwake: cls !== 'persistent', source: 'rule', label, now: await nowSec($),
  })
  tasks.set(taskId, task)
  await mirror($, task)
  await evaluate($)
  if (cls === 'unknown') void resolveUnknown($, taskId, e.command, e.description ?? '').catch(() => undefined)
}

// --- completion --------------------------------------------------------------------

async function finishTask($: Engine, taskId: string, status: string): Promise<void> {
  const task = tasks.get(taskId)
  if (!task) return
  tasks.delete(taskId)
  await mirror($, core.finish(task, status, await nowSec($)))
  await evaluate($)
}

async function reconcile(
  $: Engine,
  e: { session_id: string; background_tasks?: Array<{ id: string; status: string }> },
): Promise<void> {
  const ids = core.inFlightIds(e.background_tasks)
  const ts = await nowSec($)
  for (const task of [...tasks.values()]) {
    if (core.missingFromSnapshot(task, ts, ids, policy)) {
      tasks.delete(task.task_id)
      await mirror($, core.finish(task, 'completed', ts))
    }
  }
  await mirrorSnapshot($, e.session_id, ids)
  await evaluate($)
  // Questions that could not be asked mid-turn are asked now that a turn has ended.
  for (const item of waiting.splice(0)) {
    void resolveUnknown($, item.taskId, item.command, item.description).catch(() => undefined)
  }
}

async function endSession($: Engine, sessionId: string): Promise<void> {
  const ts = await nowSec($)
  for (const task of [...tasks.values()]) await mirror($, core.finish(task, 'completed', ts))
  tasks.clear()
  waiting.length = 0
  lastSnapshot.delete(sessionId)
  await evaluate($)
}

// --- /awake ---------------------------------------------------------------------------

async function awakeCommand($: Engine, args: string): Promise<string> {
  const [verb, ref] = args.trim().split(/\s+/)
  const now = await nowSec($)
  if (verb === 'keep' || verb === 'ignore' || verb === 'clear') {
    const found = core.matchTasks(tasks.values(), ref ?? '')
    if (found.length !== 1 || !found[0]) {
      return found.length ? `"${ref}" matches ${found.length} tasks; use more of the id.` : `No task matches "${ref ?? ''}". Run /awake to list them.`
    }
    const task = { ...found[0], override: verb === 'clear' ? null : verb } as core.Task
    tasks.set(task.task_id, task)
    await mirror($, task)
    await evaluate($)
  } else if (verb) {
    return 'Usage: /awake [keep|ignore|clear <task id>]'
  }
  const rows = [...tasks.values()].map(task => {
    const v = core.verdict(task, now, policy)
    const what = (task.description || task.command || '').split('\n')[0]?.slice(0, 50) ?? ''
    return `  ${task.task_id}  ${v.counts ? 'KEEPS AWAKE' : 'ignored'}  ${task.class}${task.label ? `/${task.label}` : ''} (${task.source}), ${core.ageText(now - task.started_at)}${v.stuck ? ', possibly stuck' : ''}\n      ${what}\n      ${v.reason}`
  })
  const head = holder ? `Holding the Mac awake (caffeinate ${holder.timed ? '-t 300, renewed' : `-w ${holder.pid}`}).` : 'Not holding the Mac awake.'
  return [head, rows.length ? 'Background tasks:' : 'No background tasks in this session.', ...rows].join('\n')
}

export const register: Register = (on, options) => {
  showStatus = options.showStatus === true
  mirrorToHerdr = options.herdrRegistry === true
  const cap = Number(options.hardCapHours)
  policy = {
    ...core.DEFAULT_POLICY,
    hardCapSec: Number.isFinite(cap) && cap > 0 ? cap * 3600 : 0,
    stuckAfterSec: Number(options.stuckAfterHours) > 0 ? Number(options.stuckAfterHours) * 3600 : core.DEFAULT_POLICY.stuckAfterSec,
    unknownFallback: options.unknownFallback === 'keep' ? 'keep' : 'ignore',
  }

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    try {
      await $.command.register({ name: 'awake', description: 'Show what keeps the Mac awake; keep, ignore or clear a task', argumentHint: '[keep|ignore|clear <task id>]' })
    } catch {
      // degrade quietly
    }
    return started
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'awake' }, async ($, e) => ({ text: await awakeCommand($, e.args) })).catch(
    ($, e, next) => next(e),
  )

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
      if (result?.status === 'async_launched' && result.agentId && !tasks.has(result.agentId)) {
        const task = core.newTask({
          taskId: result.agentId, sessionId: await $.session.id(), kind: 'subagent', description: e.description,
          cls: 'finite', keepAwake: true, source: 'rule', label: 'subagent', now: await nowSec($),
        })
        tasks.set(task.task_id, task)
        await mirror($, task)
        await evaluate($)
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
        if (note) await finishTask($, note.taskId, note.status)
      }
    } catch {
      // degrade quietly
    }
    return stored
  }).catch(($, e, next) => next(e))

  // Each Stop carries the session's in-flight background tasks: a task missing
  // from the latest list has finished.
  on('classic.Stop', async ($, e, next) => {
    const result = await next(e)
    try {
      await reconcile($, e)
    } catch {
      // degrade quietly
    }
    return result
  }).catch(($, e, next) => next(e))

  on('classic.SubagentStop', async ($, e, next) => {
    const result = await next(e)
    try {
      await reconcile($, e)
    } catch {
      // degrade quietly
    }
    return result
  }).catch(($, e, next) => next(e))

  // A session that ends takes its background work with it.
  on('session.end', async ($, e, next) => {
    const result = await next(e)
    try {
      await endSession($, e.sessionId)
    } catch {
      // degrade quietly
    }
    return result
  })
}
