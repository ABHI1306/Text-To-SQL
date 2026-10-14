import { useState, type FormEvent } from 'react'

import { callApi, useResource } from './adminApi'
import { ScreenState } from './ScreenState'
import { normalizeProposals, type SqlPair } from './adminLogic'
import type { ApiRequest } from './modelTypes'

type Instruction = { id: string; text: string; created_at?: string }
type Tab = 'SQL pairs' | 'Instructions' | 'Proposed pairs'
const TABS: Tab[] = ['SQL pairs', 'Instructions', 'Proposed pairs']

export function KnowledgeScreen({
  apiRequest,
  projectId,
  canApprove,
}: {
  apiRequest: ApiRequest
  projectId: string | undefined
  canApprove: boolean
}) {
  const [tab, setTab] = useState<Tab>('SQL pairs')
  const base = projectId ? `/api/v1/projects/${projectId}/knowledge` : null
  const proposals = useResource<unknown>(apiRequest, base && `${base}/proposals`)
  const pending = normalizeProposals(proposals.data).length

  if (!projectId || !base) {
    return (
      <section className="placeholder-content">
        <h1>Knowledge</h1>
        <p>Create a project in Setup before adding knowledge.</p>
      </section>
    )
  }

  return (
    <section className="admin-screen" aria-label="Knowledge">
      <header className="admin-header">
        <div>
          <span className="eyebrow">PROJECT KNOWLEDGE</span>
          <h1>Knowledge</h1>
          <p>Approved question and SQL pairs and standing instructions guide every answer for this project.</p>
        </div>
      </header>
      <div className="tab-bar" role="tablist" aria-label="Knowledge sections">
        {TABS.map((name) => (
          <button
            aria-controls="knowledge-panel"
            aria-selected={tab === name}
            className={tab === name ? 'active' : undefined}
            id={`knowledge-tab-${name.replace(' ', '-')}`}
            key={name}
            onClick={() => setTab(name)}
            role="tab"
            type="button"
          >
            {name}{name === 'Proposed pairs' && pending > 0 ? ` (${pending})` : ''}
          </button>
        ))}
      </div>
      <div
        aria-labelledby={`knowledge-tab-${tab.replace(' ', '-')}`}
        className="admin-panel"
        id="knowledge-panel"
        role="tabpanel"
      >
        {tab === 'SQL pairs' && <PairsTab apiRequest={apiRequest} base={base} />}
        {tab === 'Instructions' && <InstructionsTab apiRequest={apiRequest} base={base} />}
        {tab === 'Proposed pairs' && (
          <ProposalsTab apiRequest={apiRequest} base={base} canApprove={canApprove} onChanged={proposals.reload} />
        )}
      </div>
    </section>
  )
}

function PairsTab({ apiRequest, base }: { apiRequest: ApiRequest; base: string }) {
  const pairs = useResource<SqlPair[]>(apiRequest, `${base}/sql-pairs`)
  const [question, setQuestion] = useState('')
  const [sql, setSql] = useState('')
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)
  const [busy, setBusy] = useState(false)

  async function add(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setMessage(null)
    try {
      await callApi(apiRequest, `${base}/sql-pairs`, 'POST', { question: question.trim(), logical_sql: sql.trim() })
      setQuestion('')
      setSql('')
      setMessage({ text: 'SQL pair added.', error: false })
      pairs.reload()
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Could not add the pair', error: true })
    } finally {
      setBusy(false)
    }
  }

  async function remove(pair: SqlPair) {
    setMessage(null)
    try {
      await callApi(apiRequest, `${base}/sql-pairs/${pair.id}`, 'DELETE')
      setMessage({ text: 'SQL pair deleted.', error: false })
      pairs.reload()
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Could not delete the pair', error: true })
    }
  }

  const items = pairs.data

  return (
    <>
      <form className="admin-form" onSubmit={(event) => void add(event)}>
        <h2>Add an SQL pair</h2>
        <label>Question
          <input onChange={(event) => setQuestion(event.target.value)} required value={question} />
        </label>
        <label>SQL
          <textarea className="mono" onChange={(event) => setSql(event.target.value)} required rows={3} value={sql} />
        </label>
        <div><button className="primary" disabled={busy || !question.trim() || !sql.trim()} type="submit">Add pair</button></div>
      </form>
      {message && <p className={`admin-state${message.error ? ' error' : ''}`} role={message.error ? 'alert' : 'status'}>{message.text}</p>}
      <ScreenState label="SQL pairs" resource={pairs} />
      {items && items.length === 0 && <p className="admin-empty">No approved SQL pairs yet. Add one above or approve a proposal.</p>}
      {items && items.length > 0 && (
        <ul className="admin-cards" aria-label="Approved SQL pairs">
          {items.map((pair) => (
            <li key={pair.id}>
              <strong>{pair.question}</strong>
              <pre className="admin-sql">{pair.logical_sql}</pre>
              <button aria-label={`Delete pair: ${pair.question}`} onClick={() => void remove(pair)} type="button">Delete</button>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

function InstructionsTab({ apiRequest, base }: { apiRequest: ApiRequest; base: string }) {
  const instructions = useResource<Instruction[]>(apiRequest, `${base}/instructions`)
  const [text, setText] = useState('')
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null)
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)

  async function run(action: () => Promise<unknown>, done: string) {
    setMessage(null)
    try {
      await action()
      setMessage({ text: done, error: false })
      instructions.reload()
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Request failed', error: true })
    }
  }

  const items = instructions.data

  return (
    <>
      <form
        className="admin-form"
        onSubmit={(event) => {
          event.preventDefault()
          void run(async () => {
            await callApi(apiRequest, `${base}/instructions`, 'POST', { text: text.trim() })
            setText('')
          }, 'Instruction added.')
        }}
      >
        <h2>Add an instruction</h2>
        <label>Instruction
          <textarea onChange={(event) => setText(event.target.value)} required rows={2} value={text} />
        </label>
        <div><button className="primary" disabled={!text.trim()} type="submit">Add instruction</button></div>
      </form>
      {message && <p className={`admin-state${message.error ? ' error' : ''}`} role={message.error ? 'alert' : 'status'}>{message.text}</p>}
      <ScreenState label="instructions" resource={instructions} />
      {items && items.length === 0 && <p className="admin-empty">No instructions yet. Instructions are standing rules such as “revenue excludes cancelled orders”.</p>}
      {items && items.length > 0 && (
        <ul className="admin-cards" aria-label="Instructions">
          {items.map((item) => (
            <li key={item.id}>
              {editing?.id === item.id ? (
                <form
                  className="admin-form inline"
                  onSubmit={(event) => {
                    event.preventDefault()
                    void run(async () => {
                      await callApi(apiRequest, `${base}/instructions/${item.id}`, 'PATCH', { text: editing.text.trim() })
                      setEditing(null)
                    }, 'Instruction updated.')
                  }}
                >
                  <label>Edit instruction
                    <textarea onChange={(event) => setEditing({ id: item.id, text: event.target.value })} required rows={2} value={editing.text} />
                  </label>
                  <div className="admin-actions">
                    <button className="primary" disabled={!editing.text.trim()} type="submit">Save</button>
                    <button onClick={() => setEditing(null)} type="button">Cancel</button>
                  </div>
                </form>
              ) : (
                <>
                  <span>{item.text}</span>
                  <div className="admin-actions">
                    <button aria-label={`Edit instruction: ${item.text}`} onClick={() => setEditing({ id: item.id, text: item.text })} type="button">Edit</button>
                    <button
                      aria-label={`Delete instruction: ${item.text}`}
                      onClick={() => void run(() => callApi(apiRequest, `${base}/instructions/${item.id}`, 'DELETE'), 'Instruction deleted.')}
                      type="button"
                    >Delete</button>
                  </div>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

function ProposalsTab({
  apiRequest,
  base,
  canApprove,
  onChanged,
}: {
  apiRequest: ApiRequest
  base: string
  canApprove: boolean
  onChanged: () => void
}) {
  const proposals = useResource<unknown>(apiRequest, `${base}/proposals`)
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const items = proposals.data ? normalizeProposals(proposals.data) : undefined

  async function decide(id: string, decision: 'approve' | 'reject') {
    setBusyId(id)
    setMessage(null)
    try {
      await callApi(apiRequest, `${base}/proposals/${id}/${decision}`, 'POST')
      setMessage({ text: decision === 'approve' ? 'Proposal approved and added to the SQL pairs.' : 'Proposal rejected.', error: false })
      proposals.reload()
      onChanged()
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Request failed', error: true })
    } finally {
      setBusyId(null)
    }
  }

  return (
    <>
      {!canApprove && <p className="admin-state" role="status">You can review proposals but need the approve_pairs capability to decide on them.</p>}
      {message && <p className={`admin-state${message.error ? ' error' : ''}`} role={message.error ? 'alert' : 'status'}>{message.text}</p>}
      <ScreenState label="proposals" resource={proposals} />
      {items && items.length === 0 && <p className="admin-empty">No proposed pairs are waiting for review.</p>}
      {items && items.length > 0 && (
        <ul className="admin-cards" aria-label="Proposed pairs">
          {items.map((proposal) => (
            <li className="proposal" key={proposal.id}>
              <div className="proposal-compare">
                <section aria-label={`Proposal: ${proposal.question}`}>
                  <h3>Proposed</h3>
                  <strong>{proposal.question}</strong>
                  <pre className="admin-sql">{proposal.logical_sql}</pre>
                  {proposal.proposed_by && <small>Proposed by {proposal.proposed_by}</small>}
                </section>
                <section aria-label={`Nearest approved pair for: ${proposal.question}`}>
                  <h3>Nearest approved pair</h3>
                  {proposal.nearest_approved ? (
                    <>
                      <strong>{proposal.nearest_approved.question}</strong>
                      <pre className="admin-sql">{proposal.nearest_approved.logical_sql}</pre>
                    </>
                  ) : (
                    <p className="admin-empty">No similar approved pair exists.</p>
                  )}
                </section>
              </div>
              <div className="admin-actions">
                <button
                  aria-label={`Approve: ${proposal.question}`}
                  className="primary"
                  disabled={!canApprove || busyId === proposal.id}
                  onClick={() => void decide(proposal.id, 'approve')}
                  type="button"
                >Approve</button>
                <button
                  aria-label={`Reject: ${proposal.question}`}
                  disabled={!canApprove || busyId === proposal.id}
                  onClick={() => void decide(proposal.id, 'reject')}
                  type="button"
                >Reject</button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
