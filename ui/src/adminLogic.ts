export type SqlPair = { id: string; question: string; logical_sql: string; created_at?: string }
export type Proposal = {
  id: string
  question: string
  logical_sql: string
  proposed_by?: string | null
  created_at?: string
  nearest_approved: SqlPair | null
}

/** Accepts `{...proposal, nearest_approved}` or `{proposal, nearest_approved}` items. */
export function normalizeProposals(items: unknown): Proposal[] {
  if (!Array.isArray(items)) return []
  return items.map((raw) => {
    const item = raw as Record<string, unknown>
    const base = (item.proposal && typeof item.proposal === 'object' ? item.proposal : item) as Record<string, unknown>
    return {
      id: String(base.id),
      question: String(base.question ?? ''),
      logical_sql: String(base.logical_sql ?? ''),
      proposed_by: (base.proposed_by as string | null | undefined) ?? null,
      created_at: base.created_at as string | undefined,
      nearest_approved: (item.nearest_approved as SqlPair | null | undefined) ?? null,
    }
  })
}

export function diffCapabilities(
  before: readonly string[],
  after: readonly string[],
): { gained: string[]; lost: string[] } {
  return {
    gained: after.filter((name) => !before.includes(name)).sort(),
    lost: before.filter((name) => !after.includes(name)).sort(),
  }
}

export function parseAttributes(text: string): { value?: Record<string, unknown>; error?: string } {
  const trimmed = text.trim()
  if (!trimmed) return { value: {} }
  try {
    const parsed: unknown = JSON.parse(trimmed)
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { error: 'Attributes must be a JSON object such as {"region": "EU"}.' }
    }
    return { value: parsed as Record<string, unknown> }
  } catch {
    return { error: 'Attributes are not valid JSON.' }
  }
}

export function splitList(text: string): string[] {
  return [...new Set(text.split(',').map((part) => part.trim()).filter(Boolean))]
}

export function isLocked(lockedUntil: string | null | undefined, now = Date.now()): boolean {
  return Boolean(lockedUntil) && Date.parse(String(lockedUntil)) > now
}
