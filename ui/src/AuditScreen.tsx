import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'

import { callApi } from './adminApi'
import {
  auditQuery,
  emptyFilters,
  exportQuery,
  filterProblem,
  visibleWindow,
  type AuditEvent,
  type AuditFilters,
} from './auditLogic'
import type { ApiRequest } from './modelTypes'

const ROW_HEIGHT = 38
const VIEWPORT_HEIGHT = 420

type Page = { items: AuditEvent[]; nextCursor: number | null }

export function AuditScreen({ apiRequest }: { apiRequest: ApiRequest }) {
  const [draft, setDraft] = useState<AuditFilters>(emptyFilters)
  const [applied, setApplied] = useState<AuditFilters>(emptyFilters)
  const [rows, setRows] = useState<AuditEvent[]>([])
  const [cursor, setCursor] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<AuditEvent | null>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [exporting, setExporting] = useState(false)
  const generation = useRef(0)

  const load = useCallback(
    async (filters: AuditFilters, from: number | null) => {
      const mine = ++generation.current
      setLoading(true)
      setError(null)
      try {
        const page = await callApi<Page>(apiRequest, `/api/v1/audit-events?${auditQuery(filters, from)}`)
        if (mine !== generation.current) return
        setRows((current) => (from ? [...current, ...page.items] : page.items))
        setCursor(page.nextCursor)
      } catch (reason: unknown) {
        if (mine === generation.current) setError(reason instanceof Error ? reason.message : 'Could not load the audit log')
      } finally {
        if (mine === generation.current) setLoading(false)
      }
    },
    [apiRequest],
  )

  useEffect(() => {
    void load(applied, null)
  }, [applied, load])

  const problem = filterProblem(draft)

  function apply(event: FormEvent) {
    event.preventDefault()
    if (problem) return
    setScrollTop(0)
    setApplied(draft)
  }

  function clear() {
    setDraft(emptyFilters)
    setScrollTop(0)
    setApplied(emptyFilters)
  }

  async function exportCsv() {
    setExporting(true)
    setError(null)
    try {
      const response = await apiRequest(`/api/v1/audit-events/export.csv?${exportQuery(applied)}`)
      if (!response.ok) throw new Error(`Export failed (${response.status})`)
      const url = URL.createObjectURL(await response.blob())
      const link = document.createElement('a')
      link.href = url
      link.download = 'audit-events.csv'
      link.click()
      URL.revokeObjectURL(url)
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Export failed')
    } finally {
      setExporting(false)
    }
  }

  const { start, end } = visibleWindow(rows.length, scrollTop, VIEWPORT_HEIGHT, ROW_HEIGHT)

  return (
    <section className="admin-screen" aria-label="Audit">
      <header className="admin-header">
        <div>
          <span className="eyebrow">SYSTEM</span>
          <h1>Audit</h1>
          <p>Every sensitive action is recorded here. The log is append-only.</p>
        </div>
        <button disabled={exporting} onClick={() => void exportCsv()} type="button">
          {exporting ? 'Exporting…' : 'Export CSV'}
        </button>
      </header>

      <form className="admin-form audit-filters" onSubmit={apply} aria-label="Audit filters">
        <label>Actor id
          <input onChange={(e) => setDraft({ ...draft, actor: e.target.value })} placeholder="account UUID" value={draft.actor} />
        </label>
        <label>Action
          <input onChange={(e) => setDraft({ ...draft, action: e.target.value })} placeholder="e.g. login_failed" value={draft.action} />
        </label>
        <label>Target type
          <input onChange={(e) => setDraft({ ...draft, resourceType: e.target.value })} placeholder="e.g. project" value={draft.resourceType} />
        </label>
        <label>From
          <input onChange={(e) => setDraft({ ...draft, since: e.target.value })} type="datetime-local" value={draft.since} />
        </label>
        <label>To
          <input onChange={(e) => setDraft({ ...draft, until: e.target.value })} type="datetime-local" value={draft.until} />
        </label>
        <div className="admin-actions">
          <button className="primary" disabled={problem !== null} type="submit">Apply</button>
          <button onClick={clear} type="button">Clear</button>
        </div>
        {problem && <p className="admin-state error" role="alert">{problem}</p>}
      </form>

      {error && (
        <div className="admin-state error" role="alert">
          <span>{error}</span>
          <button type="button" onClick={() => void load(applied, null)}>Retry</button>
        </div>
      )}

      <div
        aria-label="Audit events"
        className="audit-table"
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
        role="table"
        style={{ height: VIEWPORT_HEIGHT }}
      >
        <div className="audit-row audit-head" role="row">
          <span role="columnheader">When</span>
          <span role="columnheader">Actor</span>
          <span role="columnheader">Action</span>
          <span role="columnheader">Target</span>
        </div>
        <div style={{ height: rows.length * ROW_HEIGHT, position: 'relative' }}>
          {rows.slice(start, end).map((item, offset) => (
            <button
              className="audit-row"
              key={item.id}
              onClick={() => setSelected(item)}
              role="row"
              style={{ position: 'absolute', top: (start + offset) * ROW_HEIGHT, height: ROW_HEIGHT }}
              type="button"
            >
              <span role="cell">{new Date(item.createdAt).toLocaleString()}</span>
              <span className="mono" role="cell">{item.actorId?.slice(0, 8) ?? 'system'}</span>
              <span role="cell">{item.action}</span>
              <span role="cell">{item.resourceType} · {item.resourceId.slice(0, 12)}</span>
            </button>
          ))}
        </div>
        {!loading && rows.length === 0 && !error && <p className="admin-empty">No events match these filters.</p>}
      </div>

      <div className="admin-actions">
        <span className="muted">{rows.length} events loaded{cursor ? ' · more available' : ''}</span>
        {cursor !== null && (
          <button disabled={loading} onClick={() => void load(applied, cursor)} type="button">
            {loading ? 'Loading…' : 'Load more'}
          </button>
        )}
      </div>

      {selected && (
        <aside aria-label="Event detail" className="audit-drawer" role="dialog">
          <header>
            <h2>{selected.action}</h2>
            <button aria-label="Close detail" onClick={() => setSelected(null)} type="button">×</button>
          </header>
          <pre className="mono">{JSON.stringify(selected, null, 2)}</pre>
        </aside>
      )}
    </section>
  )
}
