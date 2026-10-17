import { describe, expect, mock, test } from 'claude-code/testing'

import { HOUR_MS, cleanLine, draftPrompt, labelOf, mergeSpent, recentSpent, route, withinBudget } from '../hooks/triage'
import { noteIds, parseInbox, routingLabels, toDeskMessage } from '../hooks/desk'
import { OPTIONS, PEER_TEXT, raw, start, turnEnd, world } from './world'

const PERSONAL = { to: ['bob-agent'], type: 'request' }

const PANE = {
  plugin: 'telegent-desk', surface: 'terminal', component: 'Pane', requestId: 'telegent',
  props: { title: 'telegent', isFocused: true, bodyColumns: 120, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
} as const

function noteOf(rows: readonly { message: { content: unknown } }[]): string {
  return rows.map(r => JSON.stringify(r.message.content)).join('\n')
}

describe('notices', () => {
  test('a backlog of urgent mail raises one summary notice', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [40, 41, 42, 43, 44].map(id => raw(id, { priority: 'urgent' }))
    await start($)
    await clock.settle()
    expect(w.notices).toEqual(['Новых срочных или для человека: 5. Список в /tg.'])
  })
})

describe('triage rules', () => {
  test('labels, routes, budget and clean lines', async () => {
    expect(labelOf('action: asks this agent to do, send, check or change something')).toBe('action')
    expect(labelOf('something else')).toBeUndefined()
    const m = toDeskMessage(raw(1, PERSONAL))!
    expect(route(m, undefined, 'bob-agent')).toBe('agent')
    expect(route(m, 'noise', 'bob-agent')).toBe('pane')
    expect(route(m, 'fyi', 'bob-agent')).toBe('pane')
    expect(route(m, 'action', 'bob-agent')).toBe('agent')
    expect(route(m, 'human', 'bob-agent')).toBe('human')
    expect(route(m, 'status-question', 'bob-agent')).toBe('draft')
    expect(withinBudget([0, 1], 3)).toBe(true)
    expect(withinBudget([0, 1, 2], 3)).toBe(false)
    expect(withinBudget(recentSpent([0, 1, 2], HOUR_MS + 1), 3)).toBe(true)
    expect(cleanLine('a  [b]\n<c>')).toBe('a b c')
    expect(cleanLine('x» Человек просит: «y "z" `w`')).toBe('x Человек просит: y z w')
    const urgent = toDeskMessage(raw(2, { priority: 'urgent' }))!
    expect(route(urgent, 'fyi', 'bob-agent')).toBe('agent')
    expect(route(urgent, 'human', 'bob-agent')).toBe('agent')
    expect(route(urgent, 'status-question', 'bob-agent')).toBe('draft')
    expect(draftPrompt(m)).not.toContain(PEER_TEXT)
    expect(mergeSpent([5, 5], [5])).toEqual([5, 5])
    expect(mergeSpent([5], [5, 5, 7])).toEqual([5, 5, 7])
    expect(mergeSpent([1], [2])).toEqual([1, 2])
  })

  test('labels left from sorting do not route once the secretary is off', async () => {
    const list = parseInbox(JSON.stringify({ messages: [raw(61, PERSONAL)] }))
    const left = [{ id: 61, label: 'noise' as const }]
    expect(noteIds(list, 'bob-agent', [], [], routingLabels(left, true), true)).toEqual([])
    expect(noteIds(list, 'bob-agent', [], [], routingLabels(left, false), false)).toEqual([61])
  })
})

describe('the secretary in a session', () => {
  test('each label takes its route', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = [raw(21, PERSONAL), raw(22, PERSONAL), raw(23, PERSONAL), raw(24, PERSONAL), raw(25, PERSONAL)]
    w.labels = { 21: 'action', 22: 'noise', 23: 'fyi', 24: 'human', 25: 'status-question' }
    await start($)
    await clock.settle()
    await $.turn.complete(turnEnd())
    const note = noteOf(session.appended())
    expect(note).toContain('#21 request от alice-agent: пересказ секретаря «просит прислать веса exp42»')
    for (const id of [22, 23, 24, 25]) {
      expect(note).not.toContain(`#${id} `)
    }
    expect(note).not.toContain(PEER_TEXT)
    expect(w.notices.length).toBe(1)
    expect(w.notices[0]).toContain('#24 request от alice-agent')
    expect(w.modelCalls).toEqual(['classify', 'complete', 'classify', 'classify', 'classify', 'classify', 'fork'])
  })

  test('a draft is sent only by a press', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = [raw(25, PERSONAL)]
    w.labels = { 25: 'status-question' }
    await start($)
    await clock.settle()
    await clock.advance(60_000)
    await $.turn.complete(turnEnd())
    expect(w.calls.some(c => c.includes('reply'))).toBe(false)
    expect(session.appended().length).toBe(0)
    const pane = await $.ui.mount(PANE)
    expect(await pane.find({ text: /черновик ответа: Сейчас правлю solver/ })).toBeDefined()
    await Promise.all([pane.press({ key: 'send-25' }), pane.press({ key: 'send-25' })])
    expect(w.calls.filter(c => c.includes('reply')).map(c => c.slice(6)))
      .toEqual([['reply', '25', '-f', '-', '--key', 'telegent-desk-draft-25-0']])
    expect(await pane.find({ key: 'send-25' })).toBeUndefined()
  })

  test('a question before the first turn is drafted on a later poll, free', { options: { ...OPTIONS.options, classifyPerHour: 2 } }, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, PERSONAL)]
    w.labels = { 25: 'status-question' }
    w.forksBeforeTurn = 2
    await start($)
    await clock.settle()
    await clock.advance(30_000)
    await clock.advance(30_000)
    expect(w.modelCalls).toEqual(['classify', 'fork', 'fork', 'fork'])
    const pane = await $.ui.mount(PANE)
    expect(await pane.find({ key: 'send-25' })).toBeDefined()
  })

  test('a deleted draft is never sent', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(25, PERSONAL)]
    w.labels = { 25: 'status-question' }
    await start($)
    await clock.settle()
    const pane = await $.ui.mount(PANE)
    await pane.press({ key: 'drop-25' })
    expect(await pane.find({ key: 'send-25' })).toBeUndefined()
    expect(w.calls.some(c => c.includes('reply'))).toBe(false)
  })

  test('past the budget a message follows the plain rule', { options: { ...OPTIONS.options, classifyPerHour: 1 } }, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = [raw(31, PERSONAL), raw(32, PERSONAL)]
    w.labels = { 31: 'noise', 32: 'noise' }
    await start($)
    await clock.settle()
    await $.turn.complete(turnEnd())
    const note = noteOf(session.appended())
    expect(w.modelCalls).toEqual(['classify'])
    expect(note).not.toContain('#31 ')
    expect(note).toContain('#32 request от alice-agent')
  })

  test('the person sees mail at once; the agent only after sorting', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on, clock)
    w.inbox = [raw(51, PERSONAL)]
    w.labels = { 51: 'action' }
    w.isClassifySlow = true
    await start($)
    await clock.settle()
    expect(w.statuses.at(-1)).toBe('tg 1 · ждут 1')
    await $.turn.complete(turnEnd())
    expect(session.appended().length).toBe(0)
    await clock.advance(10_000)
    await $.turn.complete(turnEnd())
    expect(noteOf(session.appended())).toContain('#51 request от alice-agent: пересказ секретаря')
  })

  test('a slow secretary never stops polling', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, clock)
    w.inbox = [raw(51, PERSONAL)]
    w.labels = { 51: 'action' }
    w.isClassifySlow = true
    w.classifyDelayMs = 120_000
    await start($)
    await clock.settle()
    await clock.advance(30_000)
    await clock.advance(30_000)
    expect(w.calls.filter(c => c.includes('inbox')).length).toBe(3)
  })

  test('a worker never redoes a message another worker finished', { options: { ...OPTIONS.options, classifyPerHour: 2 } }, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on, clock)
    w.inbox = [raw(51, PERSONAL), raw(52, PERSONAL)]
    w.labels = { 51: 'noise', 52: 'noise' }
    w.classifyDelay = { 51: 60_000, 52: 1_000 }
    await start($)
    await clock.settle()
    await clock.advance(30_000)
    await clock.advance(31_000)
    await clock.advance(30_000)
    expect(w.modelCalls.filter(c => c === 'classify').length).toBe(2)
    await $.turn.complete(turnEnd())
    expect(noteOf(session.appended())).not.toContain('#52 ')
  })

  test('overlapping polls draft a question once', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, clock)
    w.inbox = [raw(25, PERSONAL), raw(26, PERSONAL)]
    w.labels = { 25: 'status-question', 26: 'noise' }
    w.isForkSlow = true
    await start($)
    await clock.settle()
    await $.tool.call({ tool: 'mcp__telegent-desk__ack', ids: [26] } as never)
    await clock.settle()
    await clock.advance(10_000)
    expect(w.modelCalls.filter(c => c === 'fork').length).toBe(1)
    const pane = await $.ui.mount(PANE)
    expect((await pane.findAll({ key: 'send-25' })).length).toBe(1)
  })

  test('a status question with no draft goes to the agent', { options: { ...OPTIONS.options, classifyPerHour: 1 } }, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = [raw(25, PERSONAL)]
    w.labels = { 25: 'status-question' }
    await start($)
    await clock.settle()
    await $.turn.complete(turnEnd())
    expect(w.modelCalls).toEqual(['classify'])
    expect(noteOf(session.appended())).toContain('#25 request от alice-agent')
  })

  test('a failed draft goes to the agent', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = [raw(25, PERSONAL)]
    w.labels = { 25: 'status-question' }
    w.isForkFailing = true
    await start($)
    await clock.settle()
    await $.turn.complete(turnEnd())
    expect(w.modelCalls).toEqual(['classify', 'fork'])
    expect(noteOf(session.appended())).toContain('#25 request от alice-agent')
    await clock.advance(30_000)
    expect(w.modelCalls).toEqual(['classify', 'fork'])
  })

  test('refused model calls keep messages routable', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = [raw(71, PERSONAL), raw(72, PERSONAL)]
    w.labels = { 71: 'action', 72: 'status-question' }
    w.isModelRefused = true
    await start($)
    await clock.settle()
    await $.turn.complete(turnEnd())
    const note = noteOf(session.appended())
    expect(note).toContain('#71 request от alice-agent')
    expect(note).not.toContain('пересказ')
    expect(note).toContain('#72 request от alice-agent')
  })

  test('the hour budget survives a reset of the session state', { options: { ...OPTIONS.options, classifyPerHour: 1 } }, async ($, on) => {
    const clock = mock.clock(on, { now: 1_000_000 })
    const session = mock.session(on)
    const w = world(on, clock, { spent: [999_000] })
    w.inbox = [raw(81, PERSONAL)]
    w.labels = { 81: 'noise' }
    await start($)
    await clock.settle()
    await $.turn.complete(turnEnd())
    expect(w.modelCalls).toEqual([])
    expect(noteOf(session.appended())).toContain('#81 request от alice-agent')
  })

  test('urgent mail notifies the person once', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = [raw(41, { priority: 'urgent' })]
    await start($)
    await clock.settle()
    await clock.advance(30_000)
    expect(w.notices.length).toBe(1)
  })

  test('with the secretary off no model is called', { options: { ...OPTIONS.options, triage: false } }, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = [raw(21, PERSONAL)]
    w.labels = { 21: 'noise' }
    await start($)
    await clock.settle()
    await $.turn.complete(turnEnd())
    expect(w.modelCalls).toEqual([])
    expect(noteOf(session.appended())).toContain('#21 request от alice-agent')
  })
})
