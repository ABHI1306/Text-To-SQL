import { useCallback, useEffect, useMemo, useState } from 'react'
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  applyNodeChanges,
  type Edge,
  type Node,
  type NodeChange,
} from 'reactflow'

import 'reactflow/dist/style.css'

type ModelDefinition = {
  name: string
  tableReference: string
  description: string
  columns: { name: string }[]
}

type RelationshipDefinition = {
  name: string
  models: [string, string]
  joinType: string
}
type Mdl = {
  version: number
  dialect: string
  models: ModelDefinition[]
  relationships: RelationshipDefinition[]
  metrics: object[]
  instructions: string[]
  layout: Record<string, { x: number; y: number }>
  project_id?: string
  revision?: number
  status?: string
}
type ApiRequest = (path: string, init?: RequestInit) => Promise<Response>
type ModelNodeData = { label: string; table: string; description: string }

function layoutFor(mdl: Mdl): Record<string, { x: number; y: number }> {
  const layout: Record<string, { x: number; y: number }> = {}
  mdl.models.forEach((model, index) => {
    layout[model.name] = mdl.layout?.[model.name] ?? {
      x: (index % 3) * 300,
      y: Math.floor(index / 3) * 200,
    }
  })
  return layout
}

export function ModelCanvas({
  apiRequest,
  projectId,
}: {
  apiRequest: ApiRequest
  projectId: string | undefined
}) {
  const [mdl, setMdl] = useState<Mdl | null>(null)
  const [nodes, setNodes] = useState<Node<ModelNodeData>[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const applyMdl = useCallback((document: Mdl) => {
    const positions = layoutFor(document)
    setMdl(document)
    setNodes(document.models.map((model) => ({
      id: model.name,
      position: positions[model.name],
      data: {
        label: model.name,
        table: model.tableReference,
        description: model.description,
      },
      type: 'default',
    })))
    setEdges(document.relationships.map((relationship) => ({
      id: relationship.name,
      source: relationship.models[0],
      target: relationship.models[1],
      label: relationship.joinType.replaceAll('_', ' '),
      animated: false,
    })))
  }, [])

  useEffect(() => {
    if (!projectId) return
    let current = true
    void apiRequest(`/api/v1/projects/${projectId}/mdl`)
      .then(async (response) => {
        if (!response.ok) throw new Error(`Could not load semantic model (${response.status})`)
        const document = await response.json() as Mdl
        if (current) applyMdl(document)
      })
      .catch((reason: unknown) => {
        if (current) setError(reason instanceof Error ? reason.message : 'Could not load model')
      })
    return () => {
      current = false
    }
  }, [apiRequest, applyMdl, projectId])

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setNodes((current) => applyNodeChanges(changes, current))
  }, [])

  const positions = useMemo(
    () => Object.fromEntries(nodes.map((node) => [node.id, node.position])),
    [nodes],
  )

  async function saveLayout() {
    if (!mdl || !projectId) return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const document = { ...mdl, layout: positions }
      delete document.project_id
      delete document.revision
      delete document.status
      const response = await apiRequest(`/api/v1/projects/${projectId}/mdl`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(document),
      })
      if (!response.ok) throw new Error(`Could not save model layout (${response.status})`)
      applyMdl(await response.json() as Mdl)
      setNotice('Model layout saved.')
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not save model layout')
    } finally {
      setBusy(false)
    }
  }

  async function deployModel() {
    if (!mdl || !projectId) return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const revision = mdl.revision ? `?revision=${mdl.revision}` : ''
      const response = await apiRequest(`/api/v1/projects/${projectId}/deploy${revision}`, {
        method: 'POST',
      })
      if (!response.ok) {
        const failure = (await response.json()) as { detail?: unknown }
        throw new Error(
          typeof failure.detail === 'string'
            ? failure.detail
            : `Could not deploy model (${response.status})`,
        )
      }
      const result = await response.json() as { version: number }
      setNotice(`Model revision ${result.version} is deployed and ready for questions.`)
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not deploy model')
    } finally {
      setBusy(false)
    }
  }

  if (!projectId) {
    return (
      <section className="placeholder-content">
        <h1>Connect and model</h1>
        <p>Create a project in Setup before editing its semantic model.</p>
      </section>
    )
  }

  return (
    <section className="model-workspace" aria-label="Semantic model editor">
      <header className="model-toolbar">
        <div>
          <span className="eyebrow">SEMANTIC MODEL</span>
          <h1>Connect and model</h1>
          <p>Drag model cards to arrange the canvas. Use the keyboard to navigate nodes.</p>
        </div>
        <div className="model-toolbar-actions">
          <button disabled={!mdl || busy} onClick={() => void saveLayout()} type="button">
            {busy ? 'Saving…' : 'Save layout'}
          </button>
          <button disabled={!mdl || busy} onClick={() => void deployModel()} type="button">
            {busy ? 'Working…' : 'Deploy model'}
          </button>
        </div>
      </header>
      {error && <p className="model-message error" role="alert">{error}</p>}
      {notice && <p className="model-message" role="status">{notice}</p>}
      {!mdl ? (
        <div className="model-empty">No saved semantic model is available for this project.</div>
      ) : (
        <div className="model-flow" role="region" aria-label="Model relationship canvas">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
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
      )}
      {mdl && (
        <ul className="model-node-list" aria-label="Models in this project">
          {mdl.models.map((model) => (
            <li key={model.name}>
              <strong>{model.name}</strong>
              <span>{model.tableReference}</span>
              <span>{model.columns.length} columns</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
