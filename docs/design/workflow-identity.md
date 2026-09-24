# Design: bind debug/rewrite sessions to one workflow tab

Produced 2026-09-24 from a diagnosis of Mike's report: "using the little bug
button at the bottom, it would start working on other workflows that I wasn't
even looking at." He typically has several ComfyUI workflow tabs open.

## Diagnosis

The agents identify "the workflow" by `session_id` alone, resolved as the
**latest row for that session** in `workflow_version`
(`backend/dao/workflow_table.py:74-87`). `session_id` is one value per browser
(`localStorage.getItem("sessionId")`), shared by every tab. Nothing records
which tab a version belongs to, and the UI applies any `workflow_update` /
`param_update` ext to whatever `app.graph` is loaded at that moment.

Concrete sequences, ranked by likelihood:

1. **Tab switch mid-debug (most likely).** `DebugGuide.tsx:148-217` captures
   `app.graphToPrompt()` once; the backend saves it and calls
   `set_request_context(session_id, None, config)` — checkpoint id is `None`.
   `get_current_workflow()` (`workflow_rewrite_tools.py:63-75`) then calls
   `get_workflow_data(session_id)` directly (not the checkpoint-aware
   `get_workflow_data_from_config`), as do `update_workflow`,
   `update_workflow_parameter`, `run_workflow`. Any newer row written during
   the run is silently adopted.
2. **Other writers clobber "latest".** `/api/save-workflow-checkpoint`
   (`conversation_api.py:360-429`) and `/api/update-workflow-ui` accept any
   `session_id` with no ownership check; `saveWorkflowCheckpointBeforeInvoke`
   is called on normal chat sends (`ChatInput` ~214). A chat message or canvas
   sync from tab B while tab A is being debugged redirects the debug.
3. **Retry loop applies late.** `MessageList.tsx:266-352` retries
   `applyNewWorkflow`/`applyParameterChanges` up to 3 times (1 s / 2 s) against
   `window.app.graph` *at retry time* — the tab active then, not when the
   message arrived.
4. **Stale checkpoint.** `debug_workflow_errors` writes a `debug_complete`
   checkpoint at the end (`debug_agent.py:593-611`) by bare session id; a
   second debug on tab B racing tab A's finish, or a leftover from earlier,
   becomes "latest".
5. The rewrite path is only partially protected: `get_workflow_data_from_config`
   honours `workflow_checkpoint_id` when set, but the debug flow never sets it.

## Fix

### (a) Carry workflow identity end-to-end
- UI helper `getActiveWorkflowIdentity()` in `ui/src/utils/graphUtils.ts`:
  reads `app.extensionManager?.workflow?.activeWorkflow` (`.path` / `.filename`
  / `.key` / `.id` where present) and a content hash of `prompt.output`
  (FNV-1a over `JSON.stringify`). Returns `{ workflow_key, workflow_hash }`.
  Fallbacks: `filename` alone if `key`/`id` absent; `"default"` if
  `extensionManager.workflow` is missing, so debug still works without
  cross-tab protection. **Verify property names against the installed
  frontend build** (`D:\ComfyUI_windows_portable\ComfyUI\web` / `comfyui_frontend`).
- `WorkflowChatAPI.saveWorkflowCheckpoint`, `saveWorkflowCheckpointBeforeInvoke`,
  `streamDebugAgent` (`workflowChatApi.ts`) take `workflowIdentity` and send
  `workflow_key` / `workflow_hash` in the body. `DebugGuide.tsx` computes it
  once at click time and passes it through.
- Backend routes `/api/debug-agent`, `/api/save-workflow-checkpoint`,
  `/api/update-workflow-ui` read the two fields and store them in
  `save_workflow_data(..., attributes={..., "workflow_key", "workflow_hash"})`,
  then `set_request_context(session_id, workflow_checkpoint_id=<saved id>, config)`
  with `workflow_key` also placed in `config`.
- `workflow_table.py`: no schema change — `attributes` is free-form JSON. Add
  `get_latest_workflow_for_key(session_id, workflow_key)` (filter in Python on
  decoded attributes; dataset is small).

### (b) Tools operate on the pinned version, never "latest for session"
- `debug_workflow_errors`: after the initial save, pin its id in the request
  context for the whole run.
- `get_current_workflow()`: use `get_workflow_data_from_config(get_config())`.
  Audit `update_workflow`, `update_workflow_parameter`, `run_workflow`
  (`backend/tools/_common.session_workflow`), `save_current_workflow`,
  `replace_node_class`: every read goes through the config resolver; every
  write creates a new version that copies `workflow_key` / `workflow_hash`
  into `attributes` **and re-pins the request context to the new id** so
  subsequent reads see the agent's own edits.

### (c) UI applies updates only to the matching tab
- Backend includes `workflow_key` and `workflow_hash` in the `data` of every
  `workflow_update` / `param_update` ext (debug agent ~540-551, rewrite tools).
- `MessageList.tsx`: before applying, compare the ext's `workflow_key` with
  `getActiveWorkflowIdentity().workflow_key`. On mismatch, do not apply; render
  a warning banner "This change belongs to workflow *X* — switch to that tab
  and click Apply", with an Apply button that re-checks and applies. Capture
  the identity when the ext is first seen, not per retry.
- `stateManagementUtils.ts` (`applyNodeParameters`, ~170): same guard.

### (d) Optional follow-up: per-tab Copilot session
Key `sessionId` by `workflow_key`. Bigger behaviour change (independent chat
per tab, interacts with `messages_${sessionId}` caching). Not in this PR.

## Tests (unittest, existing fakes)
- `test_get_workflow_data_from_config_prefers_checkpoint`: two versions under
  one session with different keys; config pins the first; resolver returns it.
- `test_get_current_workflow_uses_pinned_checkpoint`: pin, then save a newer
  row for the same session with another key; tool still returns the pinned one.
- `test_tool_write_repins_context`: `update_workflow_parameter` writes a new
  version with the same `workflow_key` and the context now points at it.
- `test_debug_run_pins_before_first_tool`: with `FakeComfy`, the checkpoint id
  is set in context before `run_workflow` executes.
- Frontend guard: no JS test runner in `ui/`; manual verification step in the
  PR: two tabs open, debug tab A, switch to B mid-run, confirm the banner
  appears and B is untouched.

## Quick mitigation (no code)
Don't touch other ComfyUI tabs while a debug run is in progress; better, have
only the target workflow tab open when pressing the bug button.
