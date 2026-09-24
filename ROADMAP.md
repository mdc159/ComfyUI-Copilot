# Roadmap

Execution order for restoring the features lost with the upstream hosted
service and extending the fork. Ordered so that each phase builds on the ones
before it. See `CHANGELOG.md` for what has shipped and
`decisions/` for why.

Status legend: `todo` · `in progress` · `done (vX.Y.Z)`

## Background (2026-09-24)

Everything that stopped working ran on the original author's hosted server
(`comfyui-copilot-server.onrender.com`, now HTTP 503). Its code was never
published, so those features must be rebuilt as local tools. What still works
is local: chat, workflow rewrite, the debug agent's fix tools, GenLab sweeps,
checkpoints, model download search.

Two findings that shaped the order:

- **The debugger never sees runtime errors.** Its `run_workflow` tool posts to
  ComfyUI's `/api/prompt`, which validates the graph and then *queues a real
  run*. The agent reads the validation reply (`node_errors: {}`), declares the
  workflow fixed, and exits; the execution error that follows (for example
  `cutlass_fp16_linear: K mismatch`) is never captured. On an 8 GB GPU most
  real failures are runtime failures, so the debugger rarely helps and costs
  a GPU run each attempt.
- **Workflows come out spread out** because the API→canvas conversion
  (`ui/src/utils/comfyuiWorkflowApi2Ui.ts`) discards existing node positions
  and applies a fixed-spacing layer layout.

## Mike's direction (2026-09-24, his words)

> "From experience I can say that the debugger doesn't work for shit so
> whatever is going on with that, that's going to need some TLC."

> "When it works on the workflows, it spreads them all out. This is a little
> detail maybe later on ... it would be nice if it could make the workflows
> more human-user-friendly when finished (for example, make them pretty)."

> "Typically this machine has just an 8 GB 4070. I will experiment with
> workflows and low resolutions, things like that, until I get them to work
> right and then rebuild them in the container to use in Runpod with cloud
> computing, higher resolution, etc. It would be awesome to be able to drive
> the Runpod ComfyUI with a local agent like Claude Code or something."

> "I would like for you to just orchestrate, not do grunt work."

> "using the little bug button down at the bottom, it would start working on other fucking workflows that I wasn't even looking at."

## Phases

| # | Phase | Status | Depends on |
|---|-------|--------|------------|
| 0 | Foundation | done (2.2.0) | — |
| 1 | Debugger sees real execution | done (2.2.0) | 0 |
| 1b | Debug binds to one workflow tab; remove fake showcase chips | in progress | 1 |
| 2 | Node search, node info, install guide (offline) | in progress | 0 |
| 3 | Web search | todo | 0 |
| 4 | Layout: keep positions, tidy new graphs | todo | — |
| 5 | Workflow library (`recall_workflow`) + Accept flow | todo | 0, 2 |
| 6 | Workflow generation (`gen_workflow`) | todo | 1, 2, 5 |
| 7a | Drive from outside: local MCP server | todo | 0–2 |
| 7b | Remote target: Runpod | todo | 0, 1, 7a |
| 8 | Downstream subgraph suggestions | todo | 5 |
| 9 | Image upload for vision, stored locally | todo | — |
| 10 | Cleanup | todo | — |

### 0. Foundation
- `backend/tools/` package for local tool implementations, one module per
  capability, each exposed as an Agents SDK `@function_tool`.
- Lift the one-call-per-connection lock in `backend/llm/service.py` so chat
  and debug runs do not queue behind each other.
- A *target* concept in `backend/utils/comfy_gateway.py`: which ComfyUI
  instance the tools talk to (URL plus optional auth), defaulting to the local
  server. Phase 7b points this at Runpod.

### 1. Debugger sees real execution
- `run_workflow` returns the validation result *and* watches the queued
  `prompt_id` (websocket or `/history`) until it finishes, capturing
  `execution_error` with node id, class, and traceback.
- A validate-only mode (no queueing) for cheap structural checks; execute mode
  is explicit and reported to the user.
- A runtime-error specialist for OOM, dtype, and VRAM failures that knows
  8 GB mitigations: lower resolution, tiled VAE, fp8/GGUF weights, `--lowvram`,
  smaller batch.
- Loop until a run completes or the coordinator reports a limitation; never
  report "complete" on validation alone.
- Tests with a fake ComfyUI that fails validation, fails at runtime, and
  succeeds.

### 1b. Debug binds to one workflow tab; remove fake showcase chips

Mike's report (2026-09-24, his words):
> "using the little bug button down at the bottom, it would start working on other fucking workflows that I wasn't even looking at."

**Problems:**
- The agents identify a workflow by the browser-wide session id and always take the newest saved version, so with several tabs open a debug run can read or write another tab's graph (design: `docs/design/workflow-identity.md`).
- The "showcase" chips shown in an empty chat (for example "debug the workflow of the current canvas") replay a pre-recorded demo conversation from `public/showcase/showcase_en.json` and never contact the backend — the recorded debug always ends with "ready for execution", which misled testing.

**Fix:**
- Carry the active tab's workflow identity on every request, pin each debug/rewrite run to the version saved at its start, and have the UI refuse to apply a change to a different tab.
- Make every chip perform the real action (the debug chip triggers the real debugger, others send their text as a real message) and delete the recorded conversations.

### 2. Node search, node info, install guide
- Build a local node index from ComfyUI-Manager's public database
  (`custom-node-list.json`, `extension-node-map.json`) plus live
  `/object_info`. Fully offline after first fetch; cached under the user
  directory.
- Tools: `search_node`, `get_node_info`, `get_node_info_by_types`. Output
  shapes match what `NodeSearch.tsx`, `NodeInstallGuide.tsx`, and the Accept
  flow already expect.
- Replaces upstream features 5 (node recommendations), 6 (node query), and
  the missing-node install guide.

### 3. Web search
- Split `bing_search` (ModelScope MCP, still answering) from
  `COPILOT_REMOTE_TOOLS` so it can be on while the dead upstream server is off.
- Add a key-based provider (Brave, Tavily, or Exa) with the key supplied
  through latchkey; keep the ModelScope endpoint as fallback.

### 4. Layout
- Preserve positions of nodes that already existed before a rewrite.
- Place new nodes next to the nodes they connect to.
- For new graphs, use a layered layout library (dagre or ELK) with compact
  node sizing instead of the fixed-spacing routine.
- Optional "Tidy" action in the chat UI.

### 5. Workflow library + Accept flow
- Index a local folder of workflows: the user's saved workflows, Comfy-Org
  templates shipped with the frontend, and anything dropped in. Store
  metadata (nodes used, resolution, models, whether it ran locally or on a
  pod).
- `recall_workflow` via embedding search using the configured model
  connection. Output matches `WorkflowOption.tsx`.
- Local `get_optimized_workflow` on Accept: substitute model files that are
  actually installed, using the debug agent's existing parameter tools.

### 6. Workflow generation
- `gen_workflow`: the LLM drafts a graph from the closest library examples
  and node info, validated node-by-node and link-by-link against
  `/object_info`, then run through the phase 1 debugger before it is shown.
- Quality depends on the model; keep the library (phase 5) as the primary
  path and generation as the fourth option, as upstream did.

### 7a. Drive from outside
- Expose the phase 0–2 tools as an MCP server on localhost so Claude Code or
  another agent can build, debug, and run workflows in this ComfyUI.

### 7b. Remote target: Runpod
- Point the gateway target at a pod (auth path to be decided: SSH tunnel or
  authenticated proxy).
- "Rebuild for cloud" helper: take a workflow that works locally at low
  resolution and apply recorded substitutions (resolution, models, batch)
  for the pod.

### 8. Downstream subgraph suggestions
- Derived from the phase 5 library: which subgraphs commonly follow a given
  node type. Output matches `DownstreamSubgraphs.tsx`.

### 9. Image upload
- Save uploads into ComfyUI's `input` folder and send inline; `provider.mjs`
  already accepts inline images.

### 10. Cleanup
- Remove announcement and usage-tracking calls from the UI.
- Fix `backend/data/workflow_rewrite_expert.json` (fails to parse at line 55).
- Replace the upstream Comfy Registry publisher ID in `pyproject.toml`.

## Working method
- Orchestrator plans and reviews; worker agents implement in isolated
  worktrees. One PR per phase (or per sub-item for the large phases), each
  with tests, a `CHANGELOG.md` entry, and a review pass before merge.
- Run tests with `python_embeded\python.exe -m unittest discover -s tests`.
