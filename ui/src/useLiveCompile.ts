import { useEffect, useState } from 'react'

import { compilePreview, type ApiRequest, type CompileResult, type Mdl } from './modelTypes'

export type LiveCompile =
  | { state: 'idle' }
  | { state: 'checking' }
  | { state: 'done'; result: CompileResult }

/** Debounced compiler check of `sql` against the draft; stale responses are dropped. */
export function useLiveCompile(
  apiRequest: ApiRequest,
  projectId: string,
  mdl: Mdl,
  sql: string | null,
  delayMs = 400,
): LiveCompile {
  const mdlKey = JSON.stringify(mdl)
  const requestKey = `${sql ?? ''}\n${mdlKey}`
  const [settled, setSettled] = useState<{ key: string; result: CompileResult } | null>(null)
  useEffect(() => {
    if (!sql) return
    let current = true
    const timer = window.setTimeout(() => {
      void compilePreview(apiRequest, projectId, JSON.parse(mdlKey) as Mdl, sql)
        .catch((): CompileResult => ({
          ok: false,
          sql: '',
          errors: [{ code: 'UNAVAILABLE', message: 'Compiler check is unavailable' }],
        }))
        .then((result) => {
          if (current) setSettled({ key: requestKey, result })
        })
    }, delayMs)
    return () => {
      current = false
      window.clearTimeout(timer)
    }
  }, [apiRequest, projectId, mdlKey, sql, delayMs, requestKey])
  if (!sql) return { state: 'idle' }
  if (settled?.key !== requestKey) return { state: 'checking' }
  return { state: 'done', result: settled.result }
}
