import type { Resource } from './adminApi'

export function ScreenState({
  resource,
  label,
}: {
  resource: Pick<Resource<unknown>, 'error' | 'loading' | 'reload'>
  label: string
}) {
  if (resource.error) {
    return (
      <div className="admin-state error" role="alert">
        <span>{resource.error}</span>
        <button type="button" onClick={resource.reload}>Retry</button>
      </div>
    )
  }
  if (resource.loading) return <p className="admin-state" role="status">Loading {label}…</p>
  return null
}
