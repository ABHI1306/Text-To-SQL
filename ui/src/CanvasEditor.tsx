import { useCallback, useMemo, useState } from 'react'
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  applyNodeChanges,
  type Connection,
  type Edge,
  type Node,
  type NodeChange,
} from 'reactflow'

import 'reactflow/dist/style.css'

import { CompileStatus } from './CompileStatus'
import {
  JOIN_TYPES,
  hasRelationship,
  inferRelationship,
  isValidName,
  type ApiRequest,
  type Column,
  type Mdl,
  type Model,
  type Relationship,
  layoutFor,
} from './modelTypes'
import { useLiveCompile } from './useLiveCompile'

type Selection = { kind: 'model' | 'relationship'; name: string } | null
type NodeData = { label: string; table: string; description: string }

export function CanvasEditor({
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
  const [selection, setSelection] = useState<Selection>(null)
  const [message, setMessage] = useState<string | null>(null)
  // Position and measured size per card; React Flow hides cards until their size is reported.
  const [measured, setMeasured] = useState<
    Record<string, { position: { x: number; y: number }; width?: number | null; height?: number | null }>
  >({})
  const nodes: Node<NodeData>[] = useMemo(() => {
    const initial = layoutFor(mdl)
    return mdl.models.map((model) => ({
      id: model.name,
      position: measured[model.name]?.position ?? initial[model.name],
      width: measured[model.name]?.width,
      height: measured[model.name]?.height,
      data: { label: model.name, table: model.tableReference, description: model.description },
      type: 'default',
    }))
  }, [mdl, measured])

  const edges: Edge[] = useMemo(
    () => mdl.relationships.map((relationship) => ({
      id: relationship.name,
      source: relationship.models[0],
      target: relationship.models[1],
      label: relationship.joinType.replaceAll('_', ' '),
      selected: selection?.kind === 'relationship' && selection.name === relationship.name,
      className: (relationship.condition ?? '').trim() === '' ? 'edge-incomplete' : undefined,
    })),
    [mdl.relationships, selection],
  )

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    const next = applyNodeChanges(changes, nodes)
    setMeasured(Object.fromEntries(next.map((node) => [
      node.id,
      { position: node.position, width: node.width, height: node.height },
    ])))
  }, [nodes])

  const onConnect = useCallback((connection: Connection) => {
    const { source, target } = connection
    if (!source || !target || source === target) return
    if (hasRelationship(mdl, source, target)) {
      setMessage(`${source} and ${target} already have a relationship.`)
      return
    }
    const relationship = inferRelationship(mdl, source, target)
    setMessage(
      relationship.condition
        ? null
        : 'Relationship added. Enter its join condition in the side panel before saving.',
    )
    onChange({ ...mdl, relationships: [...mdl.relationships, relationship] })
    setSelection({ kind: 'relationship', name: relationship.name })
  }, [mdl, onChange])

  const selectedModel = selection?.kind === 'model'
    ? mdl.models.find((model) => model.name === selection.name)
    : undefined
  const selectedRelationship = selection?.kind === 'relationship'
    ? mdl.relationships.find((relationship) => relationship.name === selection.name)
    : undefined

  return (
    <div className="canvas-editor">
      <div className="model-flow" role="region" aria-label="Model relationship canvas">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onConnect={onConnect}
          onNodeDragStop={() => {
            const layout = Object.fromEntries(
              nodes.map((node) => [node.id, { x: node.position.x, y: node.position.y }]),
            )
            if (JSON.stringify(layout) !== JSON.stringify(mdl.layout)) onChange({ ...mdl, layout })
          }}
          onNodeClick={(_, node) => setSelection({ kind: 'model', name: node.id })}
          onEdgeClick={(_, edge) => setSelection({ kind: 'relationship', name: edge.id })}
          onPaneClick={() => setSelection(null)}
          fitView
          nodesFocusable
          edgesFocusable
          fitViewOptions={{ padding: 0.2 }}
          aria-label="Semantic model relationship graph"
        >
          <MiniMap pannable zoomable />
          <Controls />
          <Background />
        </ReactFlow>
      </div>
      <aside className="side-panel" aria-label="Details panel">
        {message && <p className="model-message" role="status">{message}</p>}
        {selectedModel && (
          <ModelPanel
            apiRequest={apiRequest}
            projectId={projectId}
            mdl={mdl}
            model={selectedModel}
            onChange={onChange}
          />
        )}
        {selectedRelationship && (
          <RelationshipPanel
            apiRequest={apiRequest}
            projectId={projectId}
            mdl={mdl}
            relationship={selectedRelationship}
            onChange={onChange}
            onDeleted={() => setSelection(null)}
          />
        )}
        {!selectedModel && !selectedRelationship && (
          <p className="side-panel-hint">
            Select a model or relationship to edit it. Drag from one card's handle to another to
            create a relationship.
          </p>
        )}
      </aside>
    </div>
  )
}

function replaceModel(mdl: Mdl, name: string, change: (model: Model) => Model): Mdl {
  return { ...mdl, models: mdl.models.map((model) => (model.name === name ? change(model) : model)) }
}

function ModelPanel({
  apiRequest,
  projectId,
  mdl,
  model,
  onChange,
}: {
  apiRequest: ApiRequest
  projectId: string
  mdl: Mdl
  model: Model
  onChange: (next: Mdl) => void
}) {
  const [newName, setNewName] = useState('')
  const [newExpression, setNewExpression] = useState('')
  const calculated = model.columns.filter((column) => column.isCalculated)
  const physical = model.columns.filter((column) => !column.isCalculated)
  const update = (change: (model: Model) => Model) => onChange(replaceModel(mdl, model.name, change))
  const updateColumn = (name: string, change: Partial<Column>) =>
    update((current) => ({
      ...current,
      columns: current.columns.map((column) => (column.name === name ? { ...column, ...change } : column)),
    }))
  const nameClash = model.columns.some((column) => column.name === newName)
  const nameValid = isValidName(newName) && !nameClash

  return (
    <section aria-label={`Model ${model.name}`}>
      <span className="eyebrow">MODEL</span>
      <h2>{model.name}</h2>
      <p className="side-panel-hint">{model.tableReference}</p>
      <label className="field">
        Description
        <textarea
          value={model.description}
          onChange={(event) => update((current) => ({ ...current, description: event.target.value }))}
          rows={3}
        />
      </label>
      <label className="field">
        Primary key
        <select
          value={model.primaryKey ?? ''}
          onChange={(event) => update((current) => ({
            ...current,
            primaryKey: event.target.value || null,
            columns: current.columns.map((column) => ({
              ...column,
              isPrimaryKey: column.name === event.target.value,
            })),
          }))}
        >
          <option value="">None</option>
          {physical.map((column) => <option key={column.name} value={column.name}>{column.name}</option>)}
        </select>
      </label>
      <h3>Columns</h3>
      <ul className="column-list">
        {physical.map((column) => (
          <li key={column.name}>
            <span><strong>{column.name}</strong> <small>{column.type}</small></span>
            <input
              aria-label={`Description of ${column.name}`}
              value={column.description ?? ''}
              placeholder="Description"
              onChange={(event) => updateColumn(column.name, { description: event.target.value })}
            />
          </li>
        ))}
      </ul>
      <h3>Calculated fields</h3>
      <ul className="column-list">
        {calculated.map((column) => (
          <CalculatedField
            key={column.name}
            apiRequest={apiRequest}
            projectId={projectId}
            mdl={mdl}
            model={model}
            column={column}
            onExpression={(expression) => updateColumn(column.name, { expression })}
            onRemove={() => update((current) => ({
              ...current,
              columns: current.columns.filter((item) => item.name !== column.name),
            }))}
          />
        ))}
      </ul>
      <div className="add-row">
        <input
          aria-label="New calculated field name"
          placeholder="field_name"
          value={newName}
          onChange={(event) => setNewName(event.target.value)}
        />
        <input
          aria-label="New calculated field expression"
          placeholder="quantity * unit_price"
          value={newExpression}
          onChange={(event) => setNewExpression(event.target.value)}
        />
        <button
          type="button"
          disabled={!nameValid || newExpression.trim() === ''}
          onClick={() => {
            update((current) => ({
              ...current,
              columns: [...current.columns, {
                name: newName,
                type: 'numeric',
                isCalculated: true,
                expression: newExpression,
                description: '',
              }],
            }))
            setNewName('')
            setNewExpression('')
          }}
        >
          Add field
        </button>
      </div>
      {newName !== '' && !nameValid && (
        <p className="model-message error" role="alert">
          {nameClash
            ? `${model.name} already has a column named ${newName}.`
            : 'Use lowercase letters, digits and underscores, starting with a letter.'}
        </p>
      )}
    </section>
  )
}

function CalculatedField({
  apiRequest,
  projectId,
  mdl,
  model,
  column,
  onExpression,
  onRemove,
}: {
  apiRequest: ApiRequest
  projectId: string
  mdl: Mdl
  model: Model
  column: Column
  onExpression: (expression: string) => void
  onRemove: () => void
}) {
  const status = useLiveCompile(
    apiRequest,
    projectId,
    mdl,
    `SELECT ${column.name} FROM ${model.name}`,
  )
  return (
    <li>
      <span><strong>{column.name}</strong> <small>calculated</small></span>
      <input
        aria-label={`Expression of ${column.name}`}
        value={column.expression ?? ''}
        onChange={(event) => onExpression(event.target.value)}
      />
      <button type="button" onClick={onRemove} aria-label={`Remove ${column.name}`}>Remove</button>
      <CompileStatus status={status} />
    </li>
  )
}

function RelationshipPanel({
  apiRequest,
  projectId,
  mdl,
  relationship,
  onChange,
  onDeleted,
}: {
  apiRequest: ApiRequest
  projectId: string
  mdl: Mdl
  relationship: Relationship
  onChange: (next: Mdl) => void
  onDeleted: () => void
}) {
  const [left, right] = relationship.models
  const update = (change: Partial<Relationship>) => onChange({
    ...mdl,
    relationships: mdl.relationships.map((item) => (item.name === relationship.name ? { ...item, ...change } : item)),
  })
  const status = useLiveCompile(
    apiRequest,
    projectId,
    mdl,
    (relationship.condition ?? '').trim() === ''
      ? null
      : `SELECT 1 FROM ${left} JOIN ${right} ON ${relationship.condition}`,
  )
  return (
    <section aria-label={`Relationship ${relationship.name}`}>
      <span className="eyebrow">RELATIONSHIP</span>
      <h2>{left} → {right}</h2>
      <label className="field">
        Join type
        <select value={relationship.joinType} onChange={(event) => update({ joinType: event.target.value })}>
          {JOIN_TYPES.map((type) => <option key={type} value={type}>{type.replaceAll('_', ' ')}</option>)}
        </select>
      </label>
      <label className="field">
        Join condition
        <input
          value={relationship.condition}
          placeholder={`${left}.id = ${right}.id`}
          onChange={(event) => update({ condition: event.target.value })}
        />
      </label>
      {(relationship.condition ?? '').trim() === '' && (
        <p className="model-message error" role="alert">A join condition is required before the model can be saved.</p>
      )}
      <CompileStatus status={status} />
      <button
        type="button"
        onClick={() => {
          onChange({ ...mdl, relationships: mdl.relationships.filter((item) => item.name !== relationship.name) })
          onDeleted()
        }}
      >
        Delete relationship
      </button>
    </section>
  )
}
