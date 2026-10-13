import { useState } from 'react'

import {
  compilePreview,
  isValidName,
  type ApiRequest,
  type CompileResult,
  type Mdl,
  type Metric,
  type MetricField,
} from './modelTypes'

const emptyMetric = (base: string): Metric => ({
  name: '',
  baseObject: base,
  dimensions: [{ name: '', expression: '' }],
  measures: [{ name: '', expression: '' }],
})

function FieldRows({
  label,
  fields,
  onChange,
}: {
  label: string
  fields: MetricField[]
  onChange: (fields: MetricField[]) => void
}) {
  return (
    <fieldset className="metric-fields">
      <legend>{label}</legend>
      {fields.map((field, index) => (
        <div className="add-row" key={index}>
          <input
            aria-label={`${label} ${index + 1} name`}
            placeholder="name"
            value={field.name}
            onChange={(event) => onChange(fields.map((item, at) => (at === index ? { ...item, name: event.target.value } : item)))}
          />
          <input
            aria-label={`${label} ${index + 1} expression`}
            placeholder="expression"
            value={field.expression}
            onChange={(event) => onChange(fields.map((item, at) => (at === index ? { ...item, expression: event.target.value } : item)))}
          />
          <button
            type="button"
            aria-label={`Remove ${label.toLowerCase()} ${index + 1}`}
            onClick={() => onChange(fields.filter((_, at) => at !== index))}
          >
            Remove
          </button>
        </div>
      ))}
      <button type="button" onClick={() => onChange([...fields, { name: '', expression: '' }])}>
        Add {label.toLowerCase().replace(/s$/, '')}
      </button>
    </fieldset>
  )
}

export function MetricsEditor({
  apiRequest,
  projectId,
  mdl,
  onChange,
}: {
  apiRequest: ApiRequest
  projectId: string
  mdl: Mdl
  onChange: (next: Mdl) => void
}) {
  const [editing, setEditing] = useState<{ original: string | null; metric: Metric } | null>(null)
  const [result, setResult] = useState<CompileResult | null>(null)
  const [busy, setBusy] = useState(false)

  const metric = editing?.metric
  const fieldNames = metric ? [...metric.dimensions, ...metric.measures].map((field) => field.name) : []
  const problems: string[] = []
  if (metric) {
    if (!isValidName(metric.name)) problems.push('Metric name must be lowercase letters, digits and underscores.')
    if (mdl.metrics.some((item) => item.name === metric.name && item.name !== editing?.original)
      || mdl.models.some((model) => model.name === metric.name)) {
      problems.push(`The name ${metric.name} is already used by a model or metric.`)
    }
    if (metric.measures.length + metric.dimensions.length === 0) problems.push('Add at least one dimension or measure.')
    if (fieldNames.some((name) => !isValidName(name))) problems.push('Every dimension and measure needs a valid name.')
    if (new Set(fieldNames).size !== fieldNames.length) problems.push('Dimension and measure names must be unique.')
    if ([...metric.dimensions, ...metric.measures].some((field) => field.expression.trim() === '')) {
      problems.push('Every dimension and measure needs an expression.')
    }
  }

  function withMetric(current: Mdl, candidate: Metric, original: string | null): Mdl {
    const others = current.metrics.filter((item) => item.name !== (original ?? candidate.name))
    return { ...current, metrics: [...others, candidate] }
  }

  async function preview() {
    if (!editing) return
    setBusy(true)
    try {
      setResult(await compilePreview(
        apiRequest,
        projectId,
        withMetric(mdl, editing.metric, editing.original),
        `SELECT * FROM ${editing.metric.name}`,
        { sample: true },
      ))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="metrics-editor">
      <section aria-label="Metrics in this model">
        <h2>Metrics</h2>
        {mdl.metrics.length === 0 && <p className="side-panel-hint">No metrics yet.</p>}
        <ul className="column-list">
          {mdl.metrics.map((item) => (
            <li key={item.name}>
              <span><strong>{item.name}</strong> <small>over {item.baseObject}</small></span>
              <button type="button" onClick={() => { setEditing({ original: item.name, metric: structuredClone(item) }); setResult(null) }}>
                Edit {item.name}
              </button>
              <button
                type="button"
                onClick={() => {
                  onChange({ ...mdl, metrics: mdl.metrics.filter((other) => other.name !== item.name) })
                  if (editing?.original === item.name) setEditing(null)
                }}
              >
                Delete {item.name}
              </button>
            </li>
          ))}
        </ul>
        <button
          type="button"
          onClick={() => { setEditing({ original: null, metric: emptyMetric(mdl.models[0]?.name ?? '') }); setResult(null) }}
        >
          New metric
        </button>
      </section>
      {metric && (
        <form
          className="metric-form"
          aria-label="Metric editor"
          onSubmit={(event) => {
            event.preventDefault()
            onChange(withMetric(mdl, metric, editing?.original ?? null))
            setEditing(null)
            setResult(null)
          }}
        >
          <label className="field">
            Metric name
            <input
              value={metric.name}
              onChange={(event) => setEditing({ ...editing!, metric: { ...metric, name: event.target.value } })}
            />
          </label>
          <label className="field">
            Base model
            <select
              value={metric.baseObject}
              onChange={(event) => setEditing({ ...editing!, metric: { ...metric, baseObject: event.target.value } })}
            >
              {mdl.models.map((model) => <option key={model.name} value={model.name}>{model.name}</option>)}
            </select>
          </label>
          <FieldRows
            label="Dimensions"
            fields={metric.dimensions}
            onChange={(dimensions) => setEditing({ ...editing!, metric: { ...metric, dimensions } })}
          />
          <FieldRows
            label="Measures"
            fields={metric.measures}
            onChange={(measures) => setEditing({ ...editing!, metric: { ...metric, measures } })}
          />
          {problems.length > 0 && (
            <ul className="compile-status error" aria-label="Metric problems">
              {problems.map((problem) => <li key={problem}>{problem}</li>)}
            </ul>
          )}
          <div className="model-toolbar-actions">
            <button type="button" disabled={busy || problems.length > 0} onClick={() => void preview()}>
              {busy ? 'Compiling…' : 'Preview'}
            </button>
            <button type="submit" disabled={problems.length > 0 || (result !== null && !result.ok)}>
              Apply metric
            </button>
            <button type="button" onClick={() => { setEditing(null); setResult(null) }}>Cancel</button>
          </div>
          {result && !result.ok && (
            <ul className="compile-status error" role="alert" aria-label="Compiler errors">
              {result.errors.map((error) => (
                <li key={`${error.code}-${error.message}`}><strong>{error.code}</strong> {error.message}</li>
              ))}
            </ul>
          )}
          {result?.ok && (
            <div className="metric-result" aria-label="Compiled preview">
              <h3>Compiled SQL</h3>
              <pre>{result.sql}</pre>
              <h3>Sample result</h3>
              {result.sample?.error && <p className="model-message error">{result.sample.error}</p>}
              {result.sample?.columns && (
                <table>
                  <thead>
                    <tr>{result.sample.columns.map((column) => <th key={column}>{column}</th>)}</tr>
                  </thead>
                  <tbody>
                    {(result.sample.rows ?? []).map((row, index) => (
                      <tr key={index}>{row.map((cell, at) => <td key={at}>{String(cell ?? '')}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              )}
              {!result.sample && <p className="side-panel-hint">No sample was returned.</p>}
            </div>
          )}
        </form>
      )}
    </div>
  )
}
