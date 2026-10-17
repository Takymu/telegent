import type { On } from 'claude-code'
import { mock } from 'claude-code/testing'
import type { Engine, MockClock } from 'claude-code/testing'

export const OPTIONS = { options: { client: '/opt/telegent/tg.py', config: '/work/.telegent.json', pollSeconds: 30 } }

const USAGE = { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 }

export const PEER_TEXT = 'PEER_TEXT_DO_NOT_INJECT'

export function raw(id: number, extra: Record<string, unknown> = {}) {
  return {
    id, time: '2026-10-16T10:00:00', author: 'alice-agent', to: [], type: 'info', priority: 'normal',
    subject: PEER_TEXT, body: `${PEER_TEXT} #${id}#\nsecond line`, read: false, ...extra,
  }
}

export const INBOX = [
  raw(11),
  raw(12, { to: ['bob-agent'], type: 'request' }),
  raw(13, { priority: 'urgent', type: 'alert' }),
]

export type World = {
  calls: string[][]
  timeouts: (number | undefined)[]
  envs: (Record<string, string> | undefined)[]
  statuses: (string | undefined)[]
  isDown: boolean
  isCut: boolean
  isNextInboxSlow: boolean
  inbox: unknown[]
  labels: Record<number, string>
  modelCalls: string[]
  notices: string[]
  forksBeforeTurn: number
  isClassifySlow: boolean
  isForkFailing: boolean
  isForkSlow: boolean
  classifyDelayMs: number
  classifyDelay: Record<number, number>
  isModelRefused: boolean
  claims: unknown[]
  prompts: string[]
  stdins: (string | undefined)[]
  verdict: 'allow' | 'ask'
  isPromptDropped: boolean
  forkDelayMs: number
  acked: number[]
  thread: { id: number; author: string; type?: string; body?: string }[]
  failingReplies: number
  failingAcks: number
  states: Record<number, string>
}

export function world(on: On, clock?: MockClock, store: Record<string, unknown> = {}): World {
  mock.store(on, store)
  const w: World = { calls: [], timeouts: [], envs: [], statuses: [], isDown: false, isCut: false, isNextInboxSlow: false, inbox: INBOX,
    labels: {}, modelCalls: [], notices: [], forksBeforeTurn: 0,
    isClassifySlow: false, isForkFailing: false, isForkSlow: false, classifyDelayMs: 10_000, classifyDelay: {}, isModelRefused: false,
    claims: [], prompts: [], stdins: [], verdict: 'allow', isPromptDropped: false, forkDelayMs: 10_000,
    acked: [], thread: [], failingReplies: 0, failingAcks: 0, states: {} }
  on('process.run', async ($, e) => {
    w.calls.push([...e.argv])
    w.timeouts.push(e.init?.timeoutMs)
    w.envs.push(e.init?.env)
    w.stdins.push(e.init?.stdin)
    const args = e.argv.slice(6)
    if (w.isCut) {
      return { value: { exitCode: 0, stdout: '{"messa', stderr: '', isStdoutTruncated: true, isStderrTruncated: false } }
    }
    if (w.isDown) {
      return { value: { exitCode: 3, stdout: '', stderr: 'нет связи', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if ((args[0] === 'reply' && w.failingReplies > 0) || (args[0] === 'ack' && w.failingAcks > 0)) {
      w[args[0] === 'reply' ? 'failingReplies' : 'failingAcks'] -= 1

      return { value: { exitCode: 3, stdout: '', stderr: 'нет связи', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if (args[0] === 'read' && args.includes('--json')) {
      const id = Number(args[1])
      const stdout = JSON.stringify({
        message: { id, acked: w.acked.includes(id), status: w.states[id] ? { state: w.states[id] } : null },
        thread: w.thread,
      })

      return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    const isInbox = args[0] === 'inbox' && args[1] === '--json'
    const stdout = isInbox
      ? JSON.stringify({ messages: w.inbox })
      : args[0] === 'summary'
        ? JSON.stringify({ name: 'bob-agent', unacked_total: w.inbox.length, waiting_me: { count: 1 }, unacked: [] })
        : args[0] === 'claims'
          ? JSON.stringify({ claims: w.claims })
          : `ok ${args.join(' ')}`
    if (isInbox && w.isNextInboxSlow && clock !== undefined) {
      w.isNextInboxSlow = false
      await clock.sleep(5_000)
    }

    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('ui.status', ($, e) => {
    w.statuses.push(e.text)

    return { value: undefined }
  })
  on('command.register', () => ({ value: { command: 'tg' } }))
  on('tool.register', ($, e) => ({ value: { tool: `mcp__telegent-desk__${e.name}` } }))
  on('ui.open', () => ({ value: { isPlaced: true as const } }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('tool.check', () => ({ decision: w.verdict }))
  on('prompt.submit', ($, e) => {
    w.prompts.push(e.text)

    return w.isPromptDropped ? { drop: 'another mod refused it' } : { text: e.text }
  })
  on('model.classify', async ($, e) => {
    w.modelCalls.push('classify')
    const id = Number(/#(\d+)#/.exec(e.text)?.[1])
    const delay = w.classifyDelay[id] ?? (w.isClassifySlow ? w.classifyDelayMs : 0)
    if (delay > 0 && clock !== undefined) {
      await clock.sleep(delay)
    }
    const name = w.labels[id]
    if (name === undefined) {
      return { deny: 'model unavailable' }
    }

    return { value: e.labels.find(l => l.startsWith(`${name}:`)) }
  })
  on('model.complete', () => {
    w.modelCalls.push('complete')
    if (w.isModelRefused) {
      return { deny: 'blocked by policy' }
    }

    return { value: { isAnswered: true as const, text: 'просит  прислать\n[веса] exp42', usage: USAGE } }
  })
  on('model.fork', async () => {
    w.modelCalls.push('fork')
    if (w.forksBeforeTurn > 0) {
      w.forksBeforeTurn -= 1

      return { value: { isAnswered: false as const, reason: 'nothing-to-fork' as const } }
    }
    if (w.isModelRefused) {
      return { deny: 'blocked by policy' }
    }
    if (w.isForkSlow && clock !== undefined) {
      await clock.sleep(w.forkDelayMs)
    }
    if (w.isForkFailing) {
      return { value: { isAnswered: false as const, reason: 'empty-reply' as const, usage: USAGE } }
    }

    return { value: { isAnswered: true as const, text: 'Сейчас правлю solver, освобожусь к 18:00.', usage: USAGE } }
  })
  on('ui.notify', ($, e) => {
    w.notices.push(e.text)

    return { value: { isSent: false as const, reason: 'no-channel' as const } }
  })
  on('turn.complete', ($, e) => ({ text: e.answer }))

  return w
}

export async function start($: Engine) {
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
}

export function turnEnd() {
  return { answer: '', durationMs: 1, isAborted: false, turnId: crypto.randomUUID(), reason: 'answer' as const }
}

