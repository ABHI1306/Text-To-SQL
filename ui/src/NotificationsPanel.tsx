import { callApi, useResource } from './adminApi'
import type { ApiRequest } from './modelTypes'
import { ScreenState } from './ScreenState'

export type Notice = {
  id: string
  kind: string
  payload: Record<string, unknown>
  created_at: string
  read_at: string | null
}

/** One plain sentence per kind of notice; unknown kinds still show something readable. */
export function describeNotice(notice: Notice): string {
  const { kind, payload } = notice
  if (kind === 'scheduled_run_failed') {
    const question = typeof payload.question === 'string' ? payload.question : 'A scheduled question'
    const reason = typeof payload.message === 'string' ? `: ${payload.message}` : ''
    return `“${question}” failed to run${reason}`
  }
  if (kind === 'drift_report') return 'Profiling found new drift in your database. Open Drift and profiling to review it.'
  return kind.replaceAll('_', ' ')
}

/** The bell's list of notices, with mark-as-read. */
export function NotificationsPanel({
  apiRequest,
  onUnreadChange,
}: {
  apiRequest: ApiRequest
  onUnreadChange: (count: number) => void
}) {
  const notices = useResource<Notice[]>(apiRequest, '/api/v1/notifications?limit=50')

  async function markRead(notice: Notice) {
    await callApi(apiRequest, `/api/v1/notifications/${notice.id}/read`, 'POST')
    notices.reload()
    onUnreadChange(Math.max(0, (notices.data ?? []).filter((item) => !item.read_at && item.id !== notice.id).length))
  }

  return (
    <div aria-label="Notifications" className="notice-panel" role="dialog">
      <strong>Notices</strong>
      <ScreenState label="notices" resource={notices} />
      {notices.data && notices.data.length === 0 && <p className="admin-empty">Nothing new.</p>}
      <ul>
        {notices.data?.map((notice) => (
          <li className={notice.read_at ? 'notice read' : 'notice'} key={notice.id}>
            <span>{describeNotice(notice)}</span>
            <span className="muted">{new Date(notice.created_at).toLocaleString()}</span>
            {!notice.read_at && <button onClick={() => void markRead(notice)} type="button">Mark read</button>}
          </li>
        ))}
      </ul>
    </div>
  )
}
