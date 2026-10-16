import { useState } from 'react'

import { useResource } from './adminApi'
import type { ApiRequest } from './modelTypes'
import { ScreenState } from './ScreenState'

type Shared = {
  share_id: string
  resource_type: 'dashboard' | 'saved_question' | 'thread'
  resource_id: string
  title: string | null
  granted_at: string
}
type Tile = { id: string; chart_spec: unknown; saved_question_id: string }
type Dashboard = { id: string; title: string; tiles: Tile[] }
type TileResult = { columns: string[]; rows: unknown[][]; truncated: boolean; cached: boolean; question: string }

const LABEL: Record<Shared['resource_type'], string> = {
  dashboard: 'Dashboard',
  saved_question: 'Saved question',
  thread: 'Conversation',
}

export function SharedWithMeScreen({ apiRequest }: { apiRequest: ApiRequest }) {
  const shared = useResource<Shared[]>(apiRequest, '/api/v1/shared-with-me')
  const [open, setOpen] = useState<Shared | null>(null)

  return (
    <section className="admin-screen" aria-label="Shared with me">
      <header className="admin-header">
        <div>
          <span className="eyebrow">ASK</span>
          <h1>Shared with me</h1>
          <p>Read-only. Numbers are always computed for you, with your own data access.</p>
        </div>
      </header>
      <ScreenState label="shared items" resource={shared} />
      {shared.data && shared.data.length === 0 && <p className="admin-empty">Nothing has been shared with you yet.</p>}
      {shared.data && shared.data.length > 0 && (
        <ul className="admin-cards" aria-label="Shared items">
          {shared.data.map((item) => (
            <li key={item.share_id}>
              <div className="admin-toolbar">
                <span className="status-pill">{LABEL[item.resource_type]}</span>
                <strong>{item.title ?? 'Untitled'}</strong>
                <span className="muted">shared {new Date(item.granted_at).toLocaleDateString()}</span>
                {item.resource_type !== 'thread' && (
                  <button onClick={() => setOpen(open?.share_id === item.share_id ? null : item)} type="button">
                    {open?.share_id === item.share_id ? 'Close' : 'Open'}
                  </button>
                )}
              </div>
              {open?.share_id === item.share_id && <SharedViewer apiRequest={apiRequest} item={item} />}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function SharedViewer({ apiRequest, item }: { apiRequest: ApiRequest; item: Shared }) {
  if (item.resource_type === 'saved_question') {
    return <ResultTable apiRequest={apiRequest} path={`/api/v1/saved-questions/${item.resource_id}/result`} />
  }
  return <DashboardViewer apiRequest={apiRequest} id={item.resource_id} />
}

function DashboardViewer({ apiRequest, id }: { apiRequest: ApiRequest; id: string }) {
  const dashboard = useResource<Dashboard>(apiRequest, `/api/v1/dashboards/${id}`)
  if (dashboard.error || dashboard.loading) return <ScreenState label="dashboard" resource={dashboard} />
  if (!dashboard.data) return null
  return (
    <div className="shared-tiles">
      {dashboard.data.tiles.length === 0 && <p className="admin-empty">This dashboard has no tiles.</p>}
      {dashboard.data.tiles.map((tile) => (
        <ResultTable apiRequest={apiRequest} key={tile.id} path={`/api/v1/dashboards/${id}/tiles/${tile.id}/result`} />
      ))}
    </div>
  )
}

function ResultTable({ apiRequest, path }: { apiRequest: ApiRequest; path: string }) {
  const result = useResource<TileResult>(apiRequest, path)
  if (result.error || result.loading) return <ScreenState label="results" resource={result} />
  const data = result.data
  if (!data) return null
  return (
    <figure className="shared-tile">
      <figcaption>{data.question}{data.truncated ? ' · first rows only' : ''}</figcaption>
      <table className="admin-table">
        <thead><tr>{data.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
        <tbody>
          {data.rows.slice(0, 50).map((row, index) => (
            <tr key={index}>{row.map((cell, i) => <td key={i}>{String(cell ?? '')}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
