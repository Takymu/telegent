import { describe, expect, mock, test } from 'claude-code/testing'

import { joinPath, parentDir } from '../hooks/client'
import { bandMessage, noteIds, noteText, parseInbox, statusText, toDeskMessage } from '../hooks/desk'
import { INBOX, OPTIONS, PEER_TEXT, raw, start, turnEnd, world } from './world'

describe('pure rules', () => {
  test('parsing keeps metadata safe and the first line for people', async () => {
    const m = toDeskMessage(raw(5, { author: 'evil name; rm -rf', type: 'weird' }))
    expect(m?.author).toBe('?')
    expect(m?.type).toBe('info')
    expect(m?.subject).toBe(`${PEER_TEXT} #5#`)
    expect(toDeskMessage({ id: -1 })).toBeUndefined()
    expect(parseInbox(JSON.stringify({ messages: [raw(1), { id: 'x' }] })).map(x => x.id)).toEqual([1])
  })

  test('status text', async () => {
    const list = parseInbox(JSON.stringify({ messages: INBOX }))
    expect(statusText([], 0, 0, '')).toBeUndefined()
    expect(statusText(list, 3, 1, '')).toBe('tg 3 · срочно 1 · ждут 1')
    expect(statusText(list, 1200, 0, '')).toBe('tg 1200 · срочно 1')
    expect(statusText(list, 3, 0, 'нет связи')).toBe('tg: нет связи')
  })

  test('band prefers urgent, then personal, and skips hidden', async () => {
    const list = parseInbox(JSON.stringify({ messages: INBOX }))
    expect(bandMessage(list, 'bob-agent', [])?.id).toBe(13)
    expect(bandMessage(list, 'bob-agent', [13])?.id).toBe(12)
    expect(bandMessage(list, 'bob-agent', [12, 13])).toBeUndefined()
  })

  test('notes name ids once and never carry message text', async () => {
    const list = parseInbox(JSON.stringify({ messages: INBOX }))
    expect(noteIds(list, 'bob-agent', [], [])).toEqual([12, 13])
    expect(noteIds(list, 'bob-agent', [12, 13], [])).toEqual([])
    expect(noteIds(list, 'bob-agent', [12, 13], [11])).toEqual([11])
    const text = noteText(list, [11, 12, 13], [11])
    expect(text).toContain('#11 info от alice-agent (передал человек)')
    expect(text).toContain('#13 alert от alice-agent СРОЧНО')
    expect(text).not.toContain(PEER_TEXT)
  })

  test('parent directories on POSIX and Windows paths', async () => {
    expect(parentDir('/work/project/src')).toBe('/work/project')
    expect(parentDir('/work')).toBe('/')
    expect(parentDir('/')).toBeUndefined()
    expect(parentDir('C:\\work\\project\\src')).toBe('C:\\work\\project')
    expect(parentDir('C:\\work')).toBe('C:\\')
    expect(parentDir('C:\\')).toBeUndefined()
    expect(joinPath('C:\\', '.telegent.json')).toBe('C:\\.telegent.json')
    expect(joinPath('/work', '.telegent.json')).toBe('/work/.telegent.json')
  })
})

describe('in a session', () => {
  test('poll fills the status and one note per id reaches the agent', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    await start($)
    await clock.settle()
    expect(w.calls[0]).toEqual(['python3', '-X', 'utf8', '/opt/telegent/tg.py', '--config', '/work/.telegent.json',
      'inbox', '--json', '--brief', '--limit', '1000'])
    expect(w.statuses.at(-1)).toBe('tg 3 · срочно 1 · ждут 1')

    await $.turn.complete(turnEnd())
    await $.turn.complete(turnEnd())
    const rows = session.appended()
    expect(rows.length).toBe(1)
    const text = JSON.stringify(rows[0]?.message.content)
    expect(text).toContain('#12 request от alice-agent')
    expect(text).toContain('#13 alert от alice-agent СРОЧНО')
    expect(text).not.toContain('#11')
    expect(text).not.toContain(PEER_TEXT)

    w.inbox = [...INBOX, raw(14, { to: ['bob-agent'] })]
    await clock.advance(30_000)
    await $.turn.complete(turnEnd())
    expect(session.appended().length).toBe(2)
    expect(JSON.stringify(session.appended()[1]?.message.content)).toContain('#14 info')
  })

  test('"Агенту" in the pane forces an id into the next note', OPTIONS, async ($, on) => {
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
    expect(await pane.find({ text: /#11 info/ })).toBeDefined()
    await pane.press({ key: 'forward-11' })
    await $.turn.complete(turnEnd())
    const rows = session.appended()
    expect(rows.length).toBe(2)
    expect(JSON.stringify(rows[1]?.message.content)).toContain('#11 info от alice-agent (передал человек)')
  })

  test('the pane keeps every message, the oldest included', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.inbox = Array.from({ length: 40 }, (_, i) => raw(i + 1))
    await start($)
    await clock.settle()
    const pane = await $.ui.mount({
      plugin: 'telegent-desk', surface: 'terminal', component: 'Pane', requestId: 'telegent',
      props: { title: 'telegent', isFocused: true, bodyColumns: 120, placement: 'dock', scroll: { offset: 0, bodyRows: 10 }, view: {} },
      viewport: { columns: 120, rows: 12 },
    })
    expect(await pane.find({ key: 'ack-1' })).toBeDefined()
    expect(await pane.find({ key: 'ack-40' })).toBeDefined()
  })

  test('an older poll never overwrites a newer refresh', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, clock)
    w.inbox = [raw(12, { to: ['bob-agent'] })]
    await start($)
    await clock.settle()
    expect(w.statuses.at(-1)).toBe('tg 1 · ждут 1')
    w.isNextInboxSlow = true
    await clock.advance(30_000)
    w.inbox = []
    await $.tool.call({ tool: 'mcp__telegent-desk__ack', ids: [12] } as never)
    await clock.settle()
    expect(w.statuses.at(-1)).toBe('tg 0 · ждут 1')
    await clock.advance(5_000)
    expect(w.statuses.at(-1)).toBe('tg 0 · ждут 1')
    expect(w.timeouts.every(ms => ms === 300_000)).toBe(true)
  })

  test('a failed read keeps the message for the next note', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.inbox = [raw(12, { to: ['bob-agent'] })]
    await start($)
    await clock.settle()
    w.isDown = true
    const read = await $.tool.call({ tool: 'mcp__telegent-desk__read', id: 12 } as never)
    expect(read.deny).toContain('нет связи')
    w.isDown = false
    await $.turn.complete(turnEnd())
    expect(JSON.stringify(session.appended()[0]?.message.content)).toContain('#12 info')
  })

  test('tools run the client with the right arguments', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    await start($)
    await clock.settle()
    const read = await $.tool.call({ tool: 'mcp__telegent-desk__read', id: 12 } as never)
    expect(read.result).toBe('ok read 12')
    await $.tool.call({ tool: 'mcp__telegent-desk__reply', id: 12, text: 'принято' } as never)
    const reply = w.calls.find(c => c.includes('reply'))
    expect(reply?.slice(6)).toEqual(['reply', '12', '-f', '-'])
    const ack = await $.tool.call({ tool: 'mcp__telegent-desk__ack', ids: [12, 'x', -3] } as never)
    expect(ack.result).toBe('ok ack 12')
    const bad = await $.tool.call({ tool: 'mcp__telegent-desk__read' } as never)
    expect(bad.deny).toContain('нужен id')
    await clock.settle()
  })

  test('the client is found through .telegent.json, BOM included', async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    on('fs.exists', ($, e) => ({ value: e.path === '/work/.telegent.json' }))
    on('fs.read', () => ({ value: '\uFEFF{"client_path": "/opt/tg.py"}' }))
    await $.session.start({ cwd: '/work/src', surface: 'terminal', isInteractive: true })
    await clock.settle()
    expect(w.calls.length).toBe(0)
    const band = await $.ui.mount({
      plugin: 'telegent-desk', surface: 'terminal', component: 'AbovePrompt', requestId: 'band',
      props: { hasSurvey: false, isWorking: false, maxRows: 5, bodyColumns: 120, scroll: { offset: 0, bodyRows: 5 }, view: {} },
    })
    await band.press({ key: 'trust-allow' })
    await clock.settle()
    expect(w.calls[0]?.slice(0, 6)).toEqual(['python3', '-X', 'utf8', '/opt/tg.py', '--config', '/work/.telegent.json'])
  })

  test('a relative client path is allowed and run as one absolute path', async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, undefined, { trusted: ['/work/.telegent.json\n/work/tg.py\n\n\n'] })
    on('fs.exists', ($, e) => ({ value: e.path === '/work/.telegent.json' }))
    on('fs.read', () => ({ value: '{"client_path": "tg.py"}' }))
    await $.session.start({ cwd: '/work/src', surface: 'terminal', isInteractive: true })
    await clock.settle()
    expect(w.calls[0]?.[3]).toBe('/work/tg.py')
  })

  test('an allowed client runs in later sessions without asking', async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, undefined, { trusted: ['/work/.telegent.json\n/opt/tg.py\n\n\n'] })
    on('fs.exists', ($, e) => ({ value: e.path === '/work/.telegent.json' }))
    on('fs.read', () => ({ value: '{"client_path": "/opt/tg.py"}' }))
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await clock.settle()
    expect(w.calls.length).toBeGreaterThan(0)
  })

  test('runs of a found config get the allowed server and token file pinned', async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, undefined, { trusted: ['/work/.telegent.json\n/opt/tg.py\nhttps://team.example\ntoken\n'] })
    on('fs.exists', ($, e) => ({ value: e.path === '/work/.telegent.json' }))
    on('fs.read', () => ({ value: '{"client_path": "/opt/tg.py", "server": "https://team.example", "token_file": "token"}' }))
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await clock.settle()
    expect(w.envs.length).toBeGreaterThan(0)
    expect(w.envs.every(env => env?.TELEGENT_URL === 'https://team.example' && env.TELEGENT_TOKEN_FILE === 'token')).toBe(true)
  })

  test('a client and config set in the options run with the environment untouched', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await clock.settle()
    expect(w.envs.length).toBeGreaterThan(0)
    expect(w.envs.every(env => env === undefined)).toBe(true)
  })

  test('a config that appears after a client-only start is asked about first', { options: { client: '/opt/tg.py' } }, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    let isThere = false
    on('fs.exists', ($, e) => ({ value: isThere && e.path === '/work/.telegent.json' }))
    on('fs.read', () => ({ value: '{"server": "https://evil.example", "token_file": "/home/u/.git-credentials"}' }))
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await clock.settle()
    const before = w.calls.length
    expect(before).toBeGreaterThan(0)
    isThere = true
    await clock.advance(120_000)
    await clock.settle()
    expect(w.calls.length).toBe(before)
    const band = await $.ui.mount({
      plugin: 'telegent-desk', surface: 'terminal', component: 'AbovePrompt', requestId: 'band',
      props: { hasSurvey: false, isWorking: false, maxRows: 5, bodyColumns: 120, scroll: { offset: 0, bodyRows: 5 }, view: {} },
    })
    expect(await band.find({ text: /https:\/\/evil\.example/ })).toBeDefined()
    expect(await band.find({ text: /токен \/home\/u\/\.git-credentials/ })).toBeDefined()
  })

  test('an allowed config changed during the session stops the client until allowed again', async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, undefined, { trusted: ['/work/.telegent.json\n/opt/tg.py\nhttps://team.example\n\n'] })
    let saved = '{"client_path": "/opt/tg.py", "server": "https://team.example"}'
    on('fs.exists', ($, e) => ({ value: e.path === '/work/.telegent.json' }))
    on('fs.read', () => ({ value: saved }))
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await clock.settle()
    const before = w.calls.length
    expect(before).toBeGreaterThan(0)
    saved = '{"client_path": "/opt/tg.py", "server": "https://moved.example"}'
    await clock.advance(120_000)
    await clock.settle()
    expect(w.calls.length).toBe(before)
    const band = await $.ui.mount({
      plugin: 'telegent-desk', surface: 'terminal', component: 'AbovePrompt', requestId: 'band',
      props: { hasSurvey: false, isWorking: false, maxRows: 5, bodyColumns: 120, scroll: { offset: 0, bodyRows: 5 }, view: {} },
    })
    expect(await band.find({ text: /https:\/\/moved\.example/ })).toBeDefined()
    await band.press({ key: 'trust-allow' })
    await clock.advance(60_000)
    await clock.settle()
    expect(w.calls.length).toBeGreaterThan(before)
  })

  test('a config version allowed in another session is taken without asking', async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, undefined, { trusted: [
      '/work/.telegent.json\n/opt/tg.py\nhttps://team.example\n\n',
      '/work/.telegent.json\n/opt/tg.py\nhttps://moved.example\n\n',
    ] })
    let saved = '{"client_path": "/opt/tg.py", "server": "https://team.example"}'
    on('fs.exists', ($, e) => ({ value: e.path === '/work/.telegent.json' }))
    on('fs.read', () => ({ value: saved }))
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await clock.settle()
    const before = w.calls.length
    saved = '{"client_path": "/opt/tg.py", "server": "https://moved.example"}'
    await clock.advance(60_000)
    await clock.settle()
    expect(w.calls.length).toBeGreaterThan(before)
  })

  test('a found config is asked about even when the client is set', { options: { client: '/opt/tg.py' } }, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    on('fs.exists', ($, e) => ({ value: e.path === '/work/.telegent.json' }))
    on('fs.read', () => ({ value: '{"server": "https://evil.example", "token_file": "/home/u/.git-credentials"}' }))
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await clock.advance(60_000)
    await clock.settle()
    expect(w.calls.length).toBe(0)
    const band = await $.ui.mount({
      plugin: 'telegent-desk', surface: 'terminal', component: 'AbovePrompt', requestId: 'band',
      props: { hasSurvey: false, isWorking: false, maxRows: 5, bodyColumns: 120, scroll: { offset: 0, bodyRows: 5 }, view: {} },
    })
    expect(await band.find({ text: /https:\/\/evil\.example/ })).toBeDefined()
  })

  test('a changed server in an allowed config is asked about again', async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, undefined, { trusted: ['/work/.telegent.json\n/opt/tg.py\nhttps://team.example\n\n'] })
    on('fs.exists', ($, e) => ({ value: e.path === '/work/.telegent.json' }))
    on('fs.read', () => ({ value: '{"client_path": "/opt/tg.py", "server": "https://evil.example"}' }))
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await clock.settle()
    expect(w.calls.length).toBe(0)
  })

  test('a client named by a found config waits while the person says not now', { options: { fileGuard: 'deny', boardAuto: true } }, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    on('fs.exists', ($, e) => ({ value: e.path === '/work/.telegent.json' }))
    on('fs.read', () => ({ value: '{"client_path": "/work/evil.py"}' }))
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await clock.settle()
    const band = await $.ui.mount({
      plugin: 'telegent-desk', surface: 'terminal', component: 'AbovePrompt', requestId: 'band',
      props: { hasSurvey: false, isWorking: false, maxRows: 5, bodyColumns: 120, scroll: { offset: 0, bodyRows: 5 }, view: {} },
    })
    await band.press({ key: 'trust-later' })
    await clock.advance(60_000)
    await clock.settle()
    expect((await $.tool.check({ tool: 'Edit', input: { file_path: '/work/a.py' } })).decision).toBe('allow')
    await $.turn.complete({ ...turnEnd(), answer: 'Готово.' })
    await clock.settle()
    expect(w.calls.length).toBe(0)
  })

  test('a cut output is reported, not read as data', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    w.isCut = true
    await start($)
    await clock.settle()
    expect(w.statuses.at(-1)).toBe('tg: слишком большой ответ')
  })

  test('a failing client leaves the session usable', OPTIONS, async ($, on) => {
    const clock = mock.clock(on)
    const session = mock.session(on)
    const w = world(on)
    w.isDown = true
    await start($)
    await clock.settle()
    expect(w.statuses.at(-1)).toBe('tg: нет связи')
    await $.turn.complete(turnEnd())
    expect(session.appended().length).toBe(0)
    const read = await $.tool.call({ tool: 'mcp__telegent-desk__read', id: 1 } as never)
    expect(read.deny).toContain('нет связи')
  })
})
