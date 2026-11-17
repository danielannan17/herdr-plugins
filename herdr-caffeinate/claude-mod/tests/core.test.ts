import { expect, test } from 'claude-code/testing'
import { classify, inFlightIds, mergeRules, parseNotification, stateDir } from '../hooks/core'
import { DEFAULT_RULES } from '../hooks/rules'

// Mirrors tests/test_rules.py: the Python CLI and this mod must agree.
const PERSISTENT = [
  'npm run dev', 'pnpm dev', 'yarn start', 'bun run dev', 'next dev -p 3001', 'npx vite', 'vite --host',
  'jest --watch', 'nodemon server.js', 'tail -f app.log', 'npx serve out', 'python3 -m http.server 8000',
  'expo start', 'npx expo start --ios', 'supabase start', 'docker compose up', 'docker-compose up web',
  'tsc --watch', 'wrangler dev', 'vitest',
]
const FINITE = [
  'npm run build', 'pnpm test', 'vite build', 'vitest run', 'pytest -x', 'cargo build --release', 'cargo test',
  'xcodebuild -scheme App', 'supabase db push', 'npm run migrate', 'vercel deploy', 'tsc --noEmit', 'eslint .',
  './gradlew assembleDebug', 'jest --watchAll=false', 'npm ci',
]

test('commands classify as the Python rules do', () => {
  for (const c of PERSISTENT) expect([c, classify(c, '', DEFAULT_RULES)[0]]).toEqual([c, 'persistent'])
  for (const c of FINITE) expect([c, classify(c, '', DEFAULT_RULES)[0]]).toEqual([c, 'finite'])
  expect(classify('./run-my-thing.sh --fast', '', DEFAULT_RULES)[0]).toBe('unknown')
})

test('the description decides only when the command does not', () => {
  expect(classify('./go.sh', 'Run the test suite', DEFAULT_RULES)[0]).toBe('finite')
  expect(classify('./go.sh', 'Start the dev server', DEFAULT_RULES)[0]).toBe('persistent')
  expect(classify('npm run build', 'Start the dev server', DEFAULT_RULES)[0]).toBe('finite')
  expect(classify('./go.sh', 'Build then serve the app', DEFAULT_RULES)[0]).toBe('unknown')
})

test('user rules come first and a bad pattern is skipped', () => {
  const rules = mergeRules(DEFAULT_RULES, {
    command: { persistent: [{ name: 'broken', pattern: '(' }, { name: 'mine', pattern: 'my-daemon' }], finite: [] },
    description: { persistent: [], finite: [] },
  })
  expect(classify('my-daemon --run', '', rules)).toEqual(['persistent', 'mine'])
  expect(classify('npm run build', '', rules)[0]).toBe('finite')
})

test('notifications parse the id and how the task ended', () => {
  expect(parseNotification('<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>')).toEqual({ taskId: 'b1', status: 'completed' })
  expect(parseNotification('<task-id>b2</task-id><status>stopped</status>')).toEqual({ taskId: 'b2', status: 'killed' })
  expect(parseNotification('nothing here')).toBeNull()
})

test('only unfinished tasks are in flight', () => {
  expect(inFlightIds([{ id: 'a', status: 'running' }, { id: 'b', status: 'Completed' }, { id: 'c', status: 'pending' }])).toEqual(['a', 'c'])
  expect(inFlightIds(undefined)).toEqual([])
})

test('the state dir matches the one herdr hands the plugin', () => {
  expect(stateDir({ home: '/Users/me' })).toBe('/Users/me/.local/state/herdr/plugins/danielannan17.caffeinate')
  expect(stateDir({ home: '/Users/me', xdg: '/x' })).toBe('/x/herdr/plugins/danielannan17.caffeinate')
  expect(stateDir({ herdr: '/given', home: '/Users/me' })).toBe('/given')
})
