import { useCallback, useEffect, useState } from 'react'

import type { ApiRequest } from './modelTypes'

/** Turns any FastAPI error body (string, validation list or typed detail) into one sentence. */
export function failureMessage(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const text = detail
      .map((item: { msg?: string }) => (item.msg ?? '').replace(/^Value error, /, ''))
      .filter(Boolean)
      .join('; ')
    return text || fallback
  }
  if (detail && typeof detail === 'object') {
    const typed = detail as { message?: string; errors?: { code?: string; message?: string }[] }
    if (typed.errors?.length) {
      return typed.errors.map((error) => error.message ?? error.code ?? '').filter(Boolean).join('; ')
    }
    if (typed.message) return typed.message
  }
  const flat = body as { message?: string } | null
  return flat?.message ?? fallback
}

/** Calls the API and returns the parsed JSON body (null for 204); throws a readable Error. */
export async function callApi<T = unknown>(
  apiRequest: ApiRequest,
  path: string,
  method = 'GET',
  body?: unknown,
): Promise<T> {
  const response = await apiRequest(path, {
    method,
    headers: body === undefined ? undefined : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const parsed = response.status === 204 ? null : await response.json().catch(() => null)
  if (!response.ok) throw new Error(failureMessage(parsed, `Request failed (${response.status})`))
  return parsed as T
}

export type Resource<T> = {
  data: T | undefined
  error: string | null
  loading: boolean
  reload: () => void
}

/** Loads a GET endpoint with loading and error state; `path` null skips the request. */
export function useResource<T>(apiRequest: ApiRequest, path: string | null): Resource<T> {
  const [state, setState] = useState<{ path: string | null; data?: T; error?: string; tick: number }>({
    path: null,
    tick: -1,
  })
  const [tick, setTick] = useState(0)

  useEffect(() => {
    if (!path) return
    let current = true
    callApi<T>(apiRequest, path)
      .then((data) => {
        if (current) setState({ path, data, tick })
      })
      .catch((reason: unknown) => {
        if (current) setState({ path, error: reason instanceof Error ? reason.message : 'Request failed', tick })
      })
    return () => {
      current = false
    }
  }, [apiRequest, path, tick])

  const fresh = state.path === path && state.tick === tick
  const settled = state.path === path
  return {
    data: settled ? state.data : undefined,
    error: settled ? state.error ?? null : null,
    loading: path !== null && !fresh,
    reload: useCallback(() => setTick((value) => value + 1), []),
  }
}
