import { useState } from 'react'

import { callApi, useResource } from './adminApi'
import type { ApiRequest } from './modelTypes'
import { ScreenState } from './ScreenState'
import { ShareDialog } from './ShareDialog'

type DashboardSummary = { id: string; title: string }
type Tile = { id: string; saved_question_id: string; chart_spec: unknown; position_json: unknown }
type Dashboard = { id: string; title: string; tiles: Tile[]; is_owner: boolean }
type TileResult = {
  columns: string[]
  rows: unknown[][]
  truncated: boolean
  cached: boolean
  computed_at: string
  question: string
}

/** The owner's dashboards: a grid of tiles, each showing the viewer's own rows. */
export function DashboardsScreen({ apiRequest, projectId }: { apiRequest: ApiRequest; projectId: string | undefined }) {
  const list = useResource<DashboardSummary[]>(
    apiRequest,
    projectId ? `/api/v1/dashboards?project_id=${projectId}` : null,
  )
  const [selected, setSelected] = useState<string | null>(null)
  const [title, setTitle] = useState('')
  const [error, setError] = useState<string | null>(null)
  const activeId = selected ?? list.data?.[0]?.id ?? null

  async function create() {
    if (!projectId || !title.trim()) return
    setError(null)
    try {
      const created = await callApi<DashboardSummary>(apiRequest, '/api/v1/dashboards', 'POST', {
        project_id: projectId,
        title: title.trim(),
      })
      setTitle('')
      setSelected(created.id)
      list.reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not create the dashboard')
    }
  }

  return (
    <section aria-label="Dashboards" className="admin-screen">
      <header className="admin-header">
        <div>
          <span className="eyebrow">WORK</span>
          <h1>Dashboards</h1>
          <p>Pin answers from a conversation. Every tile is computed for whoever is looking, with their own data access.</p>
        </div>
      </header>
      <form
        className="admin-toolbar"
        onSubmit={(event) => {
          event.preventDefault()
          void create()
        }}
      >
        <input aria-label="New dashboard title" onChange={(event) => setTitle(event.target.value)} placeholder="New dashboard title" value={title} />
        <button disabled={!title.trim() || !projectId} type="submit">Create dashboard</button>
      </form>
      {error && <p className="admin-state error" role="alert">{error}</p>}
      <ScreenState label="dashboards" resource={list} />
      {list.data && list.data.length === 0 && (
        <p className="admin-empty">No dashboards yet. Create one, or use “Pin to dashboard” on an answer.</p>
      )}
      {list.data && list.data.length > 0 && (
        <div aria-label="Your dashboards" className="admin-toolbar" role="tablist">
          {list.data.map((item) => (
            <button
              aria-selected={item.id === activeId}
              key={item.id}
              onClick={() => setSelected(item.id)}
              role="tab"
              type="button"
            >
              {item.title}
            </button>
          ))}
        </div>
      )}
      {activeId && (
        <DashboardView
          apiRequest={apiRequest}
          id={activeId}
          key={activeId}
          onDeleted={() => {
            setSelected(null)
            list.reload()
          }}
          onRenamed={list.reload}
        />
      )}
    </section>
  )
}

function DashboardView({
  apiRequest,
  id,
  onDeleted,
  onRenamed,
}: {
  apiRequest: ApiRequest
  id: string
  onDeleted: () => void
  onRenamed: () => void
}) {
  const dashboard = useResource<Dashboard>(apiRequest, `/api/v1/dashboards/${id}`)
  const [generation, setGeneration] = useState(0)
  const [sharing, setSharing] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function act(action: () => Promise<string | void>) {
    setError(null)
    setNotice(null)
    try {
      const message = await action()
      if (message) setNotice(message)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Request failed')
    }
  }

  if (dashboard.error || dashboard.loading) return <ScreenState label="dashboard" resource={dashboard} />
  const data = dashboard.data
  if (!data) return null

  async function saveTiles(tiles: Tile[]) {
    await callApi(apiRequest, `/api/v1/dashboards/${id}/tiles`, 'PUT', {
      tiles: tiles.map((tile) => ({
        saved_question_id: tile.saved_question_id,
        chart_spec: tile.chart_spec ?? null,
        position: tile.position_json ?? {},
      })),
    })
    dashboard.reload()
  }

  return (
    <div className="dashboard-view">
      <div className="admin-toolbar">
        <h2>{data.title}</h2>
        {data.is_owner && (
          <>
            <button
              onClick={() => void act(async () => {
                const next = window.prompt('Dashboard title', data.title)?.trim()
                if (!next || next === data.title) return
                await callApi(apiRequest, `/api/v1/dashboards/${id}`, 'PATCH', { title: next })
                dashboard.reload()
                onRenamed()
              })}
              type="button"
            >
              Rename
            </button>
            <button
              onClick={() => void act(async () => {
                const result = await callApi<{ refreshed_tiles: number }>(apiRequest, `/api/v1/dashboards/${id}/refresh`, 'POST')
                setGeneration((value) => value + 1)
                return `Refreshed ${result.refreshed_tiles} tile${result.refreshed_tiles === 1 ? '' : 's'}.`
              })}
              type="button"
            >
              Refresh
            </button>
            <button onClick={() => setSharing((value) => !value)} type="button">Share</button>
            <button
              onClick={() => void act(async () => {
                if (!window.confirm(`Delete “${data.title}”? Its shares are removed too.`)) return
                await callApi(apiRequest, `/api/v1/dashboards/${id}`, 'DELETE')
                onDeleted()
              })}
              type="button"
            >
              Delete
            </button>
          </>
        )}
      </div>
      {notice && <p className="admin-state" role="status">{notice}</p>}
      {error && <p className="admin-state error" role="alert">{error}</p>}
      {sharing && (
        <ShareDialog
          apiRequest={apiRequest}
          onClose={() => setSharing(false)}
          resource={{ type: 'dashboard', id, title: data.title }}
        />
      )}
      {data.tiles.length === 0 && <p className="admin-empty">No tiles yet. Use “Pin to dashboard” on an answer.</p>}
      <div className="tile-grid">
        {data.tiles.map((tile, index) => (
          <TileCard
            apiRequest={apiRequest}
            dashboardId={id}
            generation={generation}
            key={`${tile.id}-${generation}`}
            onRemove={data.is_owner ? () => void act(() => saveTiles(data.tiles.filter((_, i) => i !== index))) : undefined}
            tile={tile}
          />
        ))}
      </div>
    </div>
  )
}

function TileCard({
  apiRequest,
  dashboardId,
  tile,
  onRemove,
}: {
  apiRequest: ApiRequest
  dashboardId: string
  generation: number
  tile: Tile
  onRemove?: () => void
}) {
  const result = useResource<TileResult>(apiRequest, `/api/v1/dashboards/${dashboardId}/tiles/${tile.id}/result`)
  return (
    <figure className="shared-tile tile-card">
      {result.error || result.loading ? (
        <ScreenState label="tile" resource={result} />
      ) : result.data ? (
        <>
          <figcaption>
            <strong>{result.data.question}</strong>
            <span className="status-pill">Computed for you</span>
            <span className="muted">
              {result.data.cached ? 'cached ' : 'live '}
              {new Date(result.data.computed_at).toLocaleTimeString()}
              {result.data.truncated ? ' · first rows only' : ''}
            </span>
            {onRemove && <button onClick={onRemove} type="button">Remove</button>}
          </figcaption>
          <table className="admin-table">
            <thead><tr>{result.data.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
            <tbody>
              {result.data.rows.slice(0, 50).map((row, rowIndex) => (
                <tr key={rowIndex}>{row.map((cell, i) => <td key={i}>{String(cell ?? '')}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
    </figure>
  )
}
