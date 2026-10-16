import { useCallback, useEffect, useState } from 'react'

export type ThemeChoice = 'system' | 'light' | 'dark'

const KEY = 'theme'

export function readTheme(): ThemeChoice {
  try {
    const stored = window.localStorage.getItem(KEY)
    return stored === 'light' || stored === 'dark' ? stored : 'system'
  } catch {
    return 'system'
  }
}

/** The theme actually drawn: an explicit choice, or the operating system's setting. */
export function resolveTheme(choice: ThemeChoice, systemDark: boolean): 'light' | 'dark' {
  return choice === 'system' ? (systemDark ? 'dark' : 'light') : choice
}

/** Remembers the choice, follows the system setting live, and sets data-theme on the page. */
export function useTheme(): [ThemeChoice, (choice: ThemeChoice) => void, 'light' | 'dark'] {
  const [choice, setChoice] = useState<ThemeChoice>(readTheme)
  const [resolved, setResolved] = useState<'light' | 'dark'>(() =>
    resolveTheme(readTheme(), window.matchMedia('(prefers-color-scheme: dark)').matches),
  )

  useEffect(() => {
    const query = window.matchMedia('(prefers-color-scheme: dark)')
    const apply = () => {
      const next = resolveTheme(choice, query.matches)
      document.documentElement.dataset.theme = next
      setResolved(next)
    }
    apply()
    query.addEventListener('change', apply)
    return () => query.removeEventListener('change', apply)
  }, [choice])

  const update = useCallback((next: ThemeChoice) => {
    setChoice(next)
    try {
      if (next === 'system') window.localStorage.removeItem(KEY)
      else window.localStorage.setItem(KEY, next)
    } catch {
      // Remembering the choice is a convenience only.
    }
  }, [])

  return [choice, update, resolved]
}
