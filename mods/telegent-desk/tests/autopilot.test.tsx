import { describe, expect, mock, test } from 'claude-code/testing'

import { blockingClaim, fileKey, normalKey, normalizePath, relativePath } from '../hooks/autopilot'
import { OPTIONS, raw, start, turnEnd, world } from './world'

const PERSONAL = { to: ['bob-agent'], type: 'request' }
const QUESTION = { to: ['bob-agent'], type: 'question' }

function on1(extra: Record<string, unknown>) {
  return { options: { ...OPTIONS.options, ...extra } }
}

const CLAIM = { key: 'file:src/App.py', owner: 'alice-agent', until: '18:00', expired: false }

describe('autopilot rules', () => {
  test('file keys and blocking claims', async () => {
    expect(relativePath('/work/src/app.py', '/work')).toBe('src/app.py')
    expect(relativePath('C:\\work\\src\\app.py', 'C:\\work')).toBe('src/app.py')
    expect(relativePath('/other/x.py', '/work')).toBe('/other/x.py')
    expect(fileKey('/work/src/app.py', '/work')).toBe('file:src/app.py')
    expect(fileKey('/work/src/../src/./app.py', '/work')).toBe('file:src/app.py')
    expect(normalizePath('/a/../../b')).toBe('/b')
    expect(normalizePath('C:\\w\\x\\..\\y')).toBe('C:/w/y')
    expect(normalKey('  File:SRC/app.py ')).toBe('file:src/app.py')
    const rows = [{ key: 'file:src/App.py', owner: 'alice-agent', isExpired: false }]
    expect(blockingClaim(rows, 'file:src/app.py', 'bob-agent')?.owner).toBe('alice-agent')
    expect(blockingClaim(rows, 'file:src/app.py', 'alice-agent')).toBeUndefined()
    expect(blockingClaim([{ ...rows[0]!, isExpired: true }], 'file:src/app.py', 'bob-agent')).toBeUndefined()
    const windows = [{ key: 'file:src\\App.py', owner: 'alice-agent', isExpired: false }]
    expect(blockingClaim(windows, fileKey('C:\\work\\src\\app.py', 'C:\\work'), 'bob-agent')?.owner).toBe('alice-agent')
    expect(normalKey('file:./src/x/../app.py')).toBe('file:src/app.py')
  })
})

describe('autopilot in a session', () => {
  test('the file guard asks before editing a file someone else holds', on1({ fileGuard: 'ask' }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.claims = [CLAIM]
    await start($)
    await clock.settle()
    expect(w.calls.find(c => c.includes('claims'))?.slice(6)).toEqual(['claims', '--json', '--limit', '1000'])
    const held = await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/app.py' } })
    expect(held.decision).toBe('ask')
    expect(held.reason).toContain('src/app.py занят в telegent: alice-agent.')
    expect(held.reason).not.toContain('18:00')
    const free = await $.tool.check({ tool: 'Write', input: { file_path: '/work/src/other.py' } })
    expect(free.decision).toBe('allow')
  })

  test('a link into a held file is stopped too', on1({ fileGuard: 'deny' }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    on('fs.stat', ($, e) => ({
      value: e.path === '/work/link.py'
        ? { kind: 'file' as const, size: 1, mtimeMs: 0, isLink: true, realPath: '/work/src/app.py' }
        : { kind: 'dir' as const, size: 0, mtimeMs: 0, isLink: false, realPath: e.path },
    }))
    w.claims = [CLAIM]
    await start($)
    await clock.settle()
    expect((await $.tool.check({ tool: 'Edit', input: { file_path: '/work/link.py' } })).decision).toBe('deny')
  })

  test('the guard waits for the server only briefly', on1({ fileGuard: 'deny' }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    await start($)
    await clock.settle()
    const before = w.calls.length
    await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/app.py' } })
    const i = w.calls.findIndex((c, n) => n >= before && c[6] === 'claims')
    expect(w.timeouts[i]).toBe(5_000)
  })

  test('a claim taken after the last poll still stops the edit', on1({ fileGuard: 'deny' }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    await start($)
    await clock.settle()
    w.claims = [CLAIM]
    const held = await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/app.py' } })
    expect(held.decision).toBe('deny')
  })

  test('before the first poll the guard knows the agent\'s own claims', on1({ fileGuard: 'deny' }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.claims = [{ ...CLAIM, owner: 'bob-agent' }, { ...CLAIM, key: 'file:src/b.py' }]
    w.isNextInboxSlow = true
    await start($)
    expect((await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/app.py' } })).decision).toBe('allow')
    expect((await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/b.py' } })).decision).toBe('deny')
    await clock.advance(5_000)
    await clock.settle()
  })

  test('a server outage keeps the files that were held', on1({ fileGuard: 'deny' }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.claims = [CLAIM]
    await start($)
    await clock.settle()
    w.isDown = true
    await clock.advance(31_000)
    await clock.settle()
    const asked = w.calls.filter(c => c.includes('claims')).length
    const held = await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/app.py' } })
    expect(held.decision).toBe('deny')
    expect(w.calls.filter(c => c.includes('claims')).length).toBe(asked)
  })

  test('a guard that fails stops the edit in deny mode', on1({ fileGuard: 'deny' }), async ($, on) => {
    const clock = mock.clock(on)
    world(on)
    on('fs.stat', () => ({ value: { realPath: 5 } }) as never)
    await start($)
    await clock.settle()
    const held = await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/free.py' } })
    expect(held.decision).toBe('deny')
  })

  test('the deny guard stops the edit in every mode', on1({ fileGuard: 'deny' }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.claims = [CLAIM]
    await start($)
    await clock.settle()
    const held = await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/app.py' } })
    expect(held.decision).toBe('deny')
    expect(held.reason).toContain('Согласуй правку')
  })

  test('an ask from the mode is tightened by the guard', on1({ fileGuard: 'deny' }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.claims = [CLAIM]
    w.verdict = 'ask'
    await start($)
    await clock.settle()
    expect((await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/app.py' } })).decision).toBe('deny')
    expect((await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/free.py' } })).decision).toBe('ask')
  })

  test('claim paths count from the connected project, not the session folder', { options: { fileGuard: 'deny' } }, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, undefined, { trusted: ['/work/.telegent.json\n/opt/tg.py\n\n\n'] })
    w.claims = [CLAIM]
    on('fs.exists', ($, e) => ({ value: e.path === '/work/.telegent.json' }))
    on('fs.read', () => ({ value: '{"client_path": "/opt/tg.py"}' }))
    await $.session.start({ cwd: '/work/src', surface: 'terminal', isInteractive: true })
    await clock.settle()
    expect((await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/app.py' } })).decision).toBe('deny')
  })

  test('a relative config path still finds the project root', { options: { client: '/opt/tg.py', config: './.telegent.json', fileGuard: 'deny' } }, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.claims = [CLAIM]
    await start($)
    await clock.settle()
    expect((await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/app.py' } })).decision).toBe('deny')
  })

  test('a config outside the project keeps the session folder as the root', { options: { client: '/opt/tg.py', config: '/home/u/.telegent/config.json', fileGuard: 'deny' } }, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.claims = [CLAIM]
    await start($)
    await clock.settle()
    expect((await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/app.py' } })).decision).toBe('deny')
  })

  test('without the switch the guard reads no claims and allows', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.claims = [CLAIM]
    await start($)
    await clock.settle()
    expect(w.calls.some(c => c.includes('claims'))).toBe(false)
    expect((await $.tool.check({ tool: 'Edit', input: { file_path: '/work/src/app.py' } })).decision).toBe('allow')
  })

  test('auto reply sends the draft as the secretary', on1({ autoReplyStatus: true }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, QUESTION)]
    w.labels = { 25: 'status-question' }
    await start($)
    await clock.settle()
    const i = w.calls.findIndex(c => c.includes('reply'))
    expect(w.calls[i - 1]?.slice(6)).toEqual(['read', '25', '--json', '--no-mark'])
    expect(w.calls[i]?.slice(6, 10)).toEqual(['reply', '25', '-f', '-'])
    expect(w.stdins[i]).toBe('Ответ секретаря агента: Сейчас правлю solver, освобожусь к 18:00.')
    expect(w.calls[i]?.slice(10)).toEqual(['--key', 'telegent-desk-auto-25'])
    expect(w.calls[i + 1]?.slice(6)).toEqual(['ack', '25'])
  })

  test('a request taken for a status question stays a draft for the person', on1({ autoReplyStatus: true }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, PERSONAL)]
    w.labels = { 25: 'status-question' }
    await start($)
    await clock.settle()
    expect(w.modelCalls).toContain('fork')
    expect(w.calls.some(c => c[6] === 'reply' || c[6] === 'done' || c[6] === 'ack')).toBe(false)
    const pane = await $.ui.mount({
      plugin: 'telegent-desk', surface: 'terminal', component: 'Pane', requestId: 'telegent',
      props: { title: 'telegent', isFocused: true, bodyColumns: 120, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
    })
    await pane.press({ key: 'send-25' })
    expect(w.stdins[w.calls.findIndex(c => c[6] === 'reply')]).not.toContain('секретаря')
    expect(w.calls.filter(c => c[6] === 'done').length).toBe(1)
  })

  test('a question handled while drafting is not answered', on1({ autoReplyStatus: true }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, clock)
    w.inbox = [raw(25, QUESTION)]
    w.labels = { 25: 'status-question' }
    w.isForkSlow = true
    w.forkDelayMs = 40_000
    await start($)
    await clock.settle()
    w.inbox = []
    await clock.advance(30_000)
    await clock.advance(10_000)
    expect(w.modelCalls).toContain('fork')
    expect(w.calls.some(c => c.includes('reply'))).toBe(false)
  })

  test('a question acknowledged elsewhere is not answered automatically', on1({ autoReplyStatus: true }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, QUESTION)]
    w.labels = { 25: 'status-question' }
    w.acked = [25]
    await start($)
    await clock.settle()
    expect(w.calls.some(c => c.includes('reply'))).toBe(false)
  })

  test('a question the agent already answered is not answered again', on1({ autoReplyStatus: true }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, QUESTION)]
    w.labels = { 25: 'status-question' }
    w.thread = [{ id: 25, author: 'alice-agent' }, { id: 26, author: 'bob-agent' }]
    await start($)
    await clock.settle()
    expect(w.calls.some(c => c.includes('reply'))).toBe(false)
  })

  test('a status notice of the agent in the thread is no answer', on1({ autoReplyStatus: true }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, QUESTION)]
    w.labels = { 25: 'status-question' }
    w.thread = [{ id: 25, author: 'alice-agent' }, { id: 26, author: 'bob-agent', type: 'status' }]
    await start($)
    await clock.settle()
    expect(w.calls.some(c => c.includes('reply'))).toBe(true)
  })

  test('a rejected request is not answered', on1({ autoReplyStatus: true }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, PERSONAL)]
    w.labels = { 25: 'status-question' }
    w.states = { 25: 'rejected' }
    await start($)
    await clock.settle()
    expect(w.calls.some(c => c.includes('reply') || c.includes('done'))).toBe(false)
  })

  test('a failed ack after the reply is retried by the next poll', on1({ autoReplyStatus: true }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, QUESTION)]
    w.labels = { 25: 'status-question' }
    w.failingAcks = 1
    await start($)
    await clock.settle()
    expect(w.calls.filter(c => c[6] === 'ack').length).toBe(1)
    await clock.advance(30_000)
    await clock.settle()
    expect(w.calls.filter(c => c[6] === 'reply').length).toBe(1)
    expect(w.calls.filter(c => c[6] === 'ack').length).toBe(2)
    await clock.advance(30_000)
    await clock.settle()
    expect(w.calls.filter(c => c[6] === 'ack').length).toBe(2)
  })

  test('a reply the server took while its answer was lost is finished, not dropped', on1({ autoReplyStatus: true }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, QUESTION)]
    w.labels = { 25: 'status-question' }
    w.failingReplies = 1
    await start($)
    await clock.settle()
    const body = w.calls.flatMap((c, i) => (c[6] === 'reply' ? [w.stdins[i]] : []))[0] ?? ''
    w.thread = [{ id: 25, author: 'alice-agent' }, { id: 26, author: 'bob-agent', type: 'info', body }]
    const pane = await $.ui.mount({
      plugin: 'telegent-desk', surface: 'terminal', component: 'Pane', requestId: 'telegent',
      props: { title: 'telegent', isFocused: true, bodyColumns: 120, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
    })
    await pane.press({ key: 'send-25' })
    expect(w.calls.filter(c => c[6] === 'reply').length).toBe(1)
    expect(w.calls.filter(c => c[6] === 'done').length).toBe(0)
    expect(w.calls.filter(c => c[6] === 'ack').length).toBe(1)
  })

  test('an automatic reply that failed to go out is retried by the next poll', on1({ autoReplyStatus: true }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, QUESTION)]
    w.labels = { 25: 'status-question' }
    w.failingReplies = 1
    await start($)
    await clock.settle()
    await clock.advance(30_000)
    await clock.settle()
    expect(w.calls.filter(c => c[6] === 'reply').length).toBe(2)
    expect(w.calls.filter(c => c[6] === 'ack').length).toBe(1)
  })

  test('an automatic draft remade after /clear settles a question the agent answered', on1({ autoReplyStatus: true }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, QUESTION)]
    w.labels = { 25: 'status-question' }
    w.thread = [{ id: 25, author: 'alice-agent' }, { id: 26, author: 'bob-agent', type: 'info', body: 'Другой текст' }]
    await start($)
    await clock.settle()
    expect(w.calls.some(c => c[6] === 'reply')).toBe(false)
    expect(w.calls.filter(c => c[6] === 'ack').length).toBe(1)
  })

  test('a retried automatic reply keeps its key', on1({ autoReplyStatus: true }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, QUESTION)]
    w.labels = { 25: 'status-question' }
    w.failingReplies = 1
    await start($)
    await clock.settle()
    const pane = await $.ui.mount({
      plugin: 'telegent-desk', surface: 'terminal', component: 'Pane', requestId: 'telegent',
      props: { title: 'telegent', isFocused: true, bodyColumns: 120, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
    })
    await pane.press({ key: 'send-25' })
    const keys = w.calls.filter(c => c.includes('reply')).map(c => c.at(-1))
    expect(keys).toEqual(['telegent-desk-auto-25', 'telegent-desk-auto-25'])
    const bodies = w.calls.flatMap((c, i) => (c.includes('reply') ? [w.stdins[i]] : []))
    expect(bodies[0]).toMatch(/^Ответ секретаря агента: /)
    expect(bodies[1]).toBe(bodies[0])
  })

  test('two deliveries at once name a forwarded message once', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    world(on)
    await start($)
    await clock.settle()
    await $.turn.complete(turnEnd())
    const pane = await $.ui.mount({
      plugin: 'telegent-desk', surface: 'terminal', component: 'Pane', requestId: 'telegent',
      props: { title: 'telegent', isFocused: true, bodyColumns: 120, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
    })
    await pane.press({ key: 'forward-11' })
    await Promise.all([$.turn.complete(turnEnd()), $.turn.complete(turnEnd())])
    const named = session.appended().map(r => JSON.stringify(r.message.content)).filter(t => t.includes('#11 info'))
    expect(named.length).toBe(1)
  })

  test('a forwarded message that is also for the agent is named once', on1({ triage: false }), async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = [raw(11, PERSONAL)]
    await start($)
    await clock.settle()
    const pane = await $.ui.mount({
      plugin: 'telegent-desk', surface: 'terminal', component: 'Pane', requestId: 'telegent',
      props: { title: 'telegent', isFocused: true, bodyColumns: 120, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
    })
    await pane.press({ key: 'forward-11' })
    await Promise.all([$.turn.complete(turnEnd()), $.turn.complete(turnEnd())])
    const named = session.appended().map(r => JSON.stringify(r.message.content)).filter(t => t.includes('#11 request'))
    expect(named.length).toBe(1)
  })

  test('without the switch a draft waits for a press', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, PERSONAL)]
    w.labels = { 25: 'status-question' }
    await start($)
    await clock.settle()
    expect(w.calls.some(c => c.includes('reply'))).toBe(false)
  })

  test('the board is set after a turn, at most every 10 minutes', on1({ boardAuto: true }), async ($, on) => {
    const clock = mock.clock(on, { now: 1_000_000 })
    const w = world(on)
    w.inbox = []
    await start($)
    await clock.settle()
    await $.turn.complete({ ...turnEnd(), answer: 'Поправил solver и запустил тесты.' })
    await clock.settle()
    await $.turn.complete({ ...turnEnd(), answer: 'Ещё одна правка.' })
    await clock.settle()
    const boards = w.calls.filter(c => c.includes('board'))
    expect(boards.length).toBe(1)
    expect(boards[0]?.slice(6, 9)).toEqual(['board', 'set', '--now'])
    await clock.advance(10 * 60_000)
    await $.turn.complete({ ...turnEnd(), answer: 'Третья правка.' })
    await clock.settle()
    expect(w.calls.filter(c => c.includes('board')).length).toBe(2)
  })

  test('two turn ends at once set the board once', on1({ boardAuto: true }), async ($, on) => {
    const clock = mock.clock(on, { now: 1_000_000 })
    const w = world(on)
    w.inbox = []
    await start($)
    await clock.settle()
    await Promise.all([
      $.turn.complete({ ...turnEnd(), answer: 'Первая правка.' }),
      $.turn.complete({ ...turnEnd(), answer: 'Вторая правка.' }),
    ])
    await clock.settle()
    expect(w.calls.filter(c => c.includes('board')).length).toBe(1)
  })

  test('without the switch the board is left alone', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    await start($)
    await clock.settle()
    await $.turn.complete({ ...turnEnd(), answer: 'Поправил solver.' })
    await clock.settle()
    expect(w.calls.some(c => c.includes('board'))).toBe(false)
  })

  test('an idle session is woken with a framed note', on1({ wakeIdle: true }), async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = []
    w.labels = { 21: 'action' }
    await start($)
    await clock.settle()
    w.inbox = [raw(21, PERSONAL)]
    await clock.advance(30_000)
    await clock.settle()
    expect(w.prompts.length).toBe(1)
    expect(w.prompts[0]).toContain('#21 request от alice-agent')
    expect(w.prompts[0]).not.toContain('пересказ')
    await $.turn.complete(turnEnd())
    expect(session.appended().length).toBe(0)
  })

  test('mail waiting when the session opens waits for a turn, it does not wake one', on1({ wakeIdle: true }), async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = [raw(21, PERSONAL)]
    w.labels = { 21: 'action' }
    await start($)
    await clock.settle()
    expect(w.prompts.length).toBe(0)
    await $.turn.complete(turnEnd())
    expect(JSON.stringify(session.appended()[0]?.message.content)).toContain('пересказ секретаря')
  })

  test('wakes stop after six an hour', on1({ wakeIdle: true, triage: false }), async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = []
    await start($)
    await clock.settle()
    for (let id = 30; id < 38; id += 1) {
      w.inbox = [...w.inbox, raw(id, PERSONAL)]
      await clock.advance(30_000)
      await clock.settle()
    }
    expect(w.prompts.length).toBe(6)
  })

  test('a refused wake hands the ids back to the next note', on1({ wakeIdle: true }), async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = []
    w.labels = { 21: 'action' }
    w.isPromptDropped = true
    await start($)
    await clock.settle()
    w.inbox = [raw(21, PERSONAL)]
    await clock.advance(30_000)
    await clock.settle()
    expect(w.prompts.length).toBe(1)
    await $.turn.complete(turnEnd())
    expect(JSON.stringify(session.appended()[0]?.message.content)).toContain('#21 request')
  })

  test('a busy session gets the note at the turn end, not a wake', on1({ wakeIdle: true }), async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = [raw(21, PERSONAL)]
    w.labels = { 21: 'action' }
    await $.turn.start({ text: 'работа', turnId: 'busy' })
    await start($)
    await clock.settle()
    expect(w.prompts.length).toBe(0)
    await $.turn.complete(turnEnd())
    expect(JSON.stringify(session.appended()[0]?.message.content)).toContain('#21 request')
  })

  test('without the switch an idle session is not woken', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(21, PERSONAL)]
    w.labels = { 21: 'action' }
    await start($)
    await clock.settle()
    expect(w.prompts.length).toBe(0)
  })
})
