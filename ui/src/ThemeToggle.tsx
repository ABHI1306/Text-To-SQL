import type { ThemeChoice } from './theme'

/** One-click light/dark switch for the top bar; the account menu also offers System. */
export function ThemeToggle({
  resolved,
  onTheme,
}: {
  resolved: 'light' | 'dark'
  onTheme: (choice: ThemeChoice) => void
}) {
  const next = resolved === 'dark' ? 'light' : 'dark'
  return (
    <button
      aria-label={`Switch to ${next} theme`}
      className="icon-button theme-toggle"
      onClick={() => onTheme(next)}
      title={`Switch to ${next} theme`}
      type="button"
    >
      {resolved === 'dark' ? '☀' : '☾'}
    </button>
  )
}
