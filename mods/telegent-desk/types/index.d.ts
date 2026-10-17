export type DeskMessage = {
  id: number
  author: string
  to: string[]
  type: string
  isUrgent: boolean
  subject: string
  preview: string
  isRead: boolean
}

export type Label = 'noise' | 'fyi' | 'action' | 'human' | 'status-question'

export type Route = 'agent' | 'pane' | 'human' | 'draft'

export type Labelled = { id: number; label: Label | null }

export type Written = { id: number; text: string }

// `isSent`: the reply is on the server; closing and acknowledging the question remain.
export type Draft = { id: number; text: string; key: string; isSent: boolean }

export type ClaimRow = { key: string; owner: string; isExpired: boolean }

declare module 'claude-code' {
  interface PluginState {
    'telegent-desk': {
      messages: DeskMessage[]
      me: string
      total: number
      waiting: number
      offline: string
      hidden: number[]
      forwarded: number[]
      announced: number[]
      labels: Labelled[]
      summaries: Written[]
      drafts: Draft[]
      notified: number[]
      spent: number[]
      tried: number[]
      claims: ClaimRow[]
      lastBoard: number
      trustAsk: string
      backlog: number[] | null
    }
  }
}
