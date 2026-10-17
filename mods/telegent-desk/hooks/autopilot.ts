import type { ClaimRow } from '../types'
import { safeName } from './desk'
import { cleanLine } from './triage'

export const BOARD_EVERY_MS = 10 * 60_000

// The server compares claim keys without case and extra spaces; so does the guard.
export function normalKey(key: string): string {
  const plain = key.trim().replace(/\s+/g, ' ').toLowerCase()

  return plain.startsWith('file:') ? `file:${normalizePath(plain.slice(5).trim()).replace(/^\.\//, '')}` : plain
}

// `a/./b/../c` becomes `a/c`; a leading `/` or drive stays, `..` never climbs above it.
export function normalizePath(path: string): string {
  const slashed = path.replace(/\\/g, '/')
  const head = /^([A-Za-z]:)?\//.exec(slashed)?.[0] ?? ''
  const parts: string[] = []
  for (const part of slashed.slice(head.length).split('/')) {
    if (part === '..' && parts.length > 0 && parts.at(-1) !== '..') {
      parts.pop()
    } else if (part !== '.' && part !== '' && !(part === '..' && head !== '')) {
      parts.push(part)
    }
  }

  return head + parts.join('/')
}

export function relativePath(path: string, cwd: string): string {
  const base = normalizePath(cwd).replace(/\/+$/, '')
  const full = normalizePath(path)
  const inside = full.toLowerCase().startsWith(`${base.toLowerCase()}/`)

  return (inside ? full.slice(base.length + 1) : full).replace(/^\.\//, '')
}

export function fileKey(path: string, cwd: string): string {
  return `file:${relativePath(path, cwd)}`
}

export function parseClaims(stdout: string): ClaimRow[] {
  const doc: unknown = JSON.parse(stdout)
  const list = (doc as { claims?: unknown } | null)?.claims

  return (Array.isArray(list) ? list : []).flatMap(raw => {
    const c = raw as Record<string, unknown>

    return typeof c.key === 'string' && typeof c.owner === 'string'
      ? [{ key: c.key, owner: c.owner, isExpired: c.expired === true }]
      : []
  })
}

export function blockingClaim(claims: readonly ClaimRow[], key: string, me: string): ClaimRow | undefined {
  const wanted = normalKey(key)

  return claims.find(c => normalKey(c.key) === wanted && c.owner !== me && !c.isExpired)
}

// The reason reaches the model: only the server's account name, never the claim's free text.
export function guardReason(c: ClaimRow, rel: string): string {
  return `Файл ${rel} занят в telegent: ${safeName(c.owner)}. Согласуй правку с ним или со своим человеком.`
}

export const BOARD_SYSTEM =
  'You write a status line for a team board. Answer with one line in Russian, at most 10 words: ' +
  'what the agent is doing now, from its last answer. No secrets, paths with credentials or personal data.'

export function shouldPostBoard(now: number, last: number): boolean {
  return now - last >= BOARD_EVERY_MS
}

export const SECRETARY_PREFIX = 'Ответ секретаря агента: '
