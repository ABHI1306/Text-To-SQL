import type { LiveCompile } from './useLiveCompile'

export function CompileStatus({ status }: { status: LiveCompile }) {
  if (status.state === 'idle') return null
  if (status.state === 'checking') return <p className="compile-status" role="status">Checking with the compiler…</p>
  const { result } = status
  if (result.ok) {
    return (
      <p className="compile-status ok" role="status">
        Compiles.
        <code>{result.sql}</code>
      </p>
    )
  }
  return (
    <ul className="compile-status error" role="alert" aria-label="Compiler errors">
      {result.errors.map((error) => (
        <li key={`${error.code}-${error.message}`}>
          <strong>{error.code}</strong> {error.message}
          {error.suggestions && error.suggestions.length > 0 && (
            <span> Did you mean {error.suggestions.join(', ')}?</span>
          )}
        </li>
      ))}
    </ul>
  )
}
