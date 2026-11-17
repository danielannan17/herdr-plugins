// Pure helpers for the mod: no `$`, so they can be tested without an engine.

export type Klass = 'finite' | 'persistent' | 'unknown'
export type Rule = { name?: string; pattern: string; unless?: string }
export type RuleSet = { persistent?: Rule[]; finite?: Rule[] }
export type Rules = { version?: number; command: RuleSet; description: RuleSet }

export const PLUGIN_ID = 'danielannan17.caffeinate'

// Who set a class. A lower rank never overwrites a higher one; the daemon's
// Python registry applies the same order.
export const SOURCE_RANK: Record<string, number> = { rule: 0, fork: 1, agent: 2, cli: 3, user: 3 }

export type Task = {
  task_id: string
  session_id: string
  kind: 'shell' | 'subagent' | 'monitor' | 'manual'
  command: string
  description: string
  class: Klass
  keep_awake: boolean
  source: string
  pid: number | null
  started_at: number
  updated_at: number
  status: string
  override: 'keep' | 'ignore' | null
  label: string | null
  timeout: number | null
  snapshot_tracked: boolean
  stuck_notified_at?: number
}

export function stateDir(env: { herdr?: string; xdg?: string; home?: string }): string {
  if (env.herdr) return env.herdr
  const base = env.xdg || `${env.home ?? ''}/.local/state`
  return `${base}/herdr/plugins/${PLUGIN_ID}`
}

export function configDir(env: { herdr?: string; home?: string }): string {
  return env.herdr || `${env.home ?? ''}/.config/herdr/plugins/config/${PLUGIN_ID}`
}

const safe = (part: string): string => (part || 'none').replace(/[^A-Za-z0-9_.-]/g, '_')

export const taskPath = (dir: string, session: string, taskId: string): string =>
  `${dir}/tasks/${safe(session)}__${safe(taskId)}.json`

/** The start of every task file name of one session. */
export const sessionPrefix = (session: string): string => `${safe(session)}__`

export const snapshotPath = (dir: string, session: string): string =>
  `${dir}/snapshots/${safe(session)}.json`

export const normalize = (command: string): string => (command || '').trim().replace(/\s+/g, ' ')

function firstHit(text: string, rules: Rule[] | undefined): string | null {
  for (const rule of rules ?? []) {
    try {
      if (new RegExp(rule.pattern, 'i').test(text) && !(rule.unless && new RegExp(rule.unless, 'i').test(text))) {
        return rule.name ?? rule.pattern
      }
    } catch {
      // a bad user rule must not break classification
    }
  }
  return null
}

function classifyText(text: string, set: RuleSet): [Klass, string | null] {
  const persistent = firstHit(text, set.persistent)
  if (persistent) return ['persistent', persistent]
  const finite = firstHit(text, set.finite)
  if (finite) return ['finite', finite]
  return ['unknown', null]
}

/** The command decides when a rule matches it; otherwise the description, if it points one way. */
export function classify(command: string, description: string, rules: Rules): [Klass, string | null] {
  const [cls, label] = classifyText(command, rules.command)
  if (cls !== 'unknown') return [cls, label]
  const persistent = firstHit(description, rules.description.persistent)
  const finite = firstHit(description, rules.description.finite)
  if (persistent && !finite) return ['persistent', persistent]
  if (finite && !persistent) return ['finite', finite]
  return ['unknown', null]
}

/** User rules go first, as the Python side merges them. */
export function mergeRules(base: Rules, user: Partial<Rules> | null): Rules {
  if (!user) return base
  const join = (a: RuleSet | undefined, b: RuleSet | undefined): RuleSet => ({
    persistent: [...(a?.persistent ?? []), ...(b?.persistent ?? [])],
    finite: [...(a?.finite ?? []), ...(b?.finite ?? [])],
  })
  return { ...base, command: join(user.command, base.command), description: join(user.description, base.description) }
}

export function newTask(f: {
  taskId: string
  sessionId: string
  kind: Task['kind']
  command?: string
  description?: string
  cls: Klass
  keepAwake?: boolean
  source: string
  label?: string | null
  now: number
}): Task {
  return {
    task_id: f.taskId,
    session_id: f.sessionId,
    kind: f.kind,
    command: f.command ?? '',
    description: f.description ?? '',
    class: f.cls,
    keep_awake: f.keepAwake ?? true,
    source: f.source,
    pid: null,
    started_at: f.now,
    updated_at: f.now,
    status: 'running',
    override: null,
    label: f.label ?? null,
    timeout: null,
    snapshot_tracked: true,
  }
}

/** Apply a classification from `source`, unless something outranks it. */
export function reclassify(
  task: Task,
  change: { cls: Klass; keepAwake: boolean; source: string; label?: string | null },
  now: number,
): Task {
  if ((SOURCE_RANK[change.source] ?? 0) < (SOURCE_RANK[task.source] ?? 0)) return task
  return {
    ...task,
    class: change.cls,
    keep_awake: change.keepAwake,
    source: change.source,
    label: change.label ?? task.label,
    updated_at: now,
  }
}

export function finish(task: Task, status: string, now: number): Task {
  return { ...task, status, updated_at: now }
}

/** `<task-notification>` rows: the task id and how it ended, or null for any other text. */
export function parseNotification(text: string): { taskId: string; status: string } | null {
  const taskId = /<task-id>\s*([^<\s]+)\s*<\/task-id>/.exec(text)?.[1]
  if (!taskId) return null
  const status = /<status>\s*([a-z_]+)\s*<\/status>/.exec(text)?.[1] ?? 'completed'
  return { taskId, status: status === 'stopped' ? 'killed' : status }
}

const ENDED = new Set(['completed', 'failed', 'killed', 'stopped', 'cancelled', 'canceled', 'error'])

/** Ids of the background tasks a Stop hook reports as still in flight. */
export function inFlightIds(tasks: ReadonlyArray<{ id: string; status: string }> | undefined): string[] {
  return (tasks ?? []).filter(t => !ENDED.has(String(t.status).toLowerCase())).map(t => t.id)
}

export function forkPrompt(command: string, description: string): string {
  return [
    'Out-of-band question about a background command you started. Answer with one JSON object and nothing else; do not use tools.',
    `Command: ${command.slice(0, 600)}`,
    description ? `Your description of it: ${description.slice(0, 300)}` : '',
    'Is it finite work whose completion matters (a build, test run, migration, deploy, code generation, a background subagent), or a persistent service that runs until stopped (dev server, file watcher, tunnel, log tail)?',
    'Answer exactly: {"class":"finite"|"persistent","keep_awake":true|false,"reason":"a few words"}',
    'keep_awake is true only when the Mac should stay awake until this command finishes.',
  ]
    .filter(Boolean)
    .join('\n')
}

export type ForkAnswer = { cls: 'finite' | 'persistent'; keepAwake: boolean; reason: string }

export function parseForkAnswer(text: string): ForkAnswer | null {
  const match = /\{[\s\S]*\}/.exec(text)
  if (!match) return null
  try {
    const raw = JSON.parse(match[0]) as Record<string, unknown>
    if (raw.class !== 'finite' && raw.class !== 'persistent') return null
    return {
      cls: raw.class,
      keepAwake: raw.class === 'finite' ? raw.keep_awake !== false : false,
      reason: typeof raw.reason === 'string' ? raw.reason.slice(0, 120) : '',
    }
  } catch {
    return null
  }
}

export type Policy = {
  unknownGraceSec: number
  unknownFallback: 'ignore' | 'keep'
  stuckAfterSec: number
  hardCapSec: number // 0: no cap
  snapshotGraceSec: number
}

export const DEFAULT_POLICY: Policy = {
  unknownGraceSec: 120,
  unknownFallback: 'ignore',
  stuckAfterSec: 6 * 3600,
  hardCapSec: 0,
  snapshotGraceSec: 5,
}

export type Verdict = { counts: boolean; reason: string; stuck: boolean }

const BY: Record<string, string> = {
  rule: 'by pattern rule',
  agent: 'by its agent',
  fork: 'by asking its agent',
  cli: 'by CLI',
  user: 'by you',
}

/** Whether a task keeps the Mac awake, and why. A user override beats everything. */
export function verdict(task: Task, nowSec: number, policy: Policy): Verdict {
  const age = Math.max(0, nowSec - task.started_at)
  const by = BY[task.source] ?? task.source
  let counts: boolean
  let reason: string
  let stuckOk = true
  if (task.override === 'keep') {
    ;[counts, reason, stuckOk] = [true, 'kept awake by your override', false]
  } else if (task.override === 'ignore') {
    ;[counts, reason] = [false, 'ignored by your override']
  } else if (task.class === 'persistent') {
    ;[counts, reason] = [false, `persistent service (${by})`]
  } else if (task.class === 'finite') {
    ;[counts, reason] = task.keep_awake
      ? [true, `finite work (${by})`]
      : [false, `finite, but its agent said it need not keep awake`]
  } else if (age < policy.unknownGraceSec) {
    ;[counts, reason] = [true, `unclassified, counted for ${Math.ceil(policy.unknownGraceSec - age)}s more`]
  } else if (policy.unknownFallback === 'keep') {
    ;[counts, reason] = [true, 'unclassified, fallback is keep awake']
  } else {
    ;[counts, reason] = [false, 'unclassified, fallback is ignore']
  }
  return { counts, reason, stuck: counts && stuckOk && age > policy.stuckAfterSec }
}

/** Past the optional hard cap, and not kept by the user. */
export const overHardCap = (task: Task, nowSec: number, policy: Policy): boolean =>
  policy.hardCapSec > 0 && task.override !== 'keep' && nowSec - task.started_at > policy.hardCapSec

/** Absent from the session's latest in-flight list, and old enough that the list can be trusted. */
export function missingFromSnapshot(task: Task, snapshotTs: number, ids: readonly string[], policy: Policy): boolean {
  return (
    task.snapshot_tracked &&
    task.started_at + policy.snapshotGraceSec < snapshotTs &&
    !ids.includes(task.task_id)
  )
}

export function ageText(seconds: number): string {
  if (seconds < 90) return `${Math.floor(seconds)}s`
  if (seconds < 5400) return `${Math.floor(seconds / 60)}m`
  return `${Math.floor(seconds / 3600)}h${String(Math.floor((seconds % 3600) / 60)).padStart(2, '0')}m`
}

/** Tasks matching an id or a unique id prefix. */
export const matchTasks = (tasks: Iterable<Task>, ref: string): Task[] => {
  const all = [...tasks]
  const exact = all.filter(t => t.task_id === ref)
  return exact.length ? exact : all.filter(t => ref !== '' && t.task_id.startsWith(ref))
}
