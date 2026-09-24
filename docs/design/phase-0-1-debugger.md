# Design: Phase 0 (Foundation) and Phase 1 (Debugger sees real execution)

Produced 2026-09-24 by the planning agent, reviewed by the orchestrator.
Targets ComfyUI 0.37.0, openai-agents 0.22.2, aiohttp 3.14.3.

Orchestrator adjustments to the original design:
- The three existing tool modules under `backend/service/` are **not** moved in
  this phase. New tools go in `backend/tools/`; migration is Phase 10.
- PRs do not edit `CHANGELOG.md` or the version. The scribe records merges;
  the orchestrator bumps the version at deploy.

## Findings that shape the design

1. **Validation without queueing exists in-process.**
   `execution.validate_prompt(prompt_id, prompt, partial_execution_list)`
   (`ComfyUI/execution.py:1128`) is async and returns
   `(valid, error, good_outputs, node_errors)`. `post_prompt` (`server.py:1115`)
   calls it on the aiohttp loop, the same loop our routes run on. Caveats: it
   mutates the prompt (`execution.py:990-1003`), so deep-copy first; and
   `post_prompt` runs `self.node_replace_manager.apply_replacements(prompt)`
   beforehand (`server.py:1113`), which we mirror. There is no HTTP
   validate-only endpoint in ComfyUI. Design choice: expose validation as a
   Copilot-owned route `POST /api/copilot/validate` and have the gateway always
   call it over HTTP, so local and remote targets share one code path and the
   test double can serve the route.

2. **`/history/{prompt_id}` already contains everything.** `PromptQueue.task_done`
   stores `status: {status_str: 'success'|'error', completed, messages: [[event, data], ...]}`
   (`execution.py:1321-1340`); `messages` includes the full `execution_error`
   payload `{node_id, node_type, exception_type, exception_message, traceback (list[str]), executed, current_inputs, current_outputs}`
   (`execution.py:701-712`), or `execution_interrupted`. Polling `/history` plus
   `/queue` is sufficient; the websocket is not needed for Phase 1.

3. **Targeted cancel exists on 0.37:** `POST /interrupt {"prompt_id"}` interrupts
   only if that prompt is running (`server.py:1171-1191`); `POST /queue {"delete":[id]}`
   removes a pending one.

4. **Lock behaviour today.** `ModelService.events` holds the per-connection
   `threading.Lock` for the whole subprocess lifetime. The SDK closes the model
   stream before running tools (`agents/run_internal/model_retry.py:281-283`), so
   a tool calling `complete_sync` is not a deadlock today; the cost is that chat
   and debug on the same connection serialize. `provider.mjs:61-66` guarantees
   every credential refresh is emitted **before any model output**, which allows
   releasing the lock at the first non-credential event.

5. `update_workflow_parameter` (`parameter_tools.py:484`) stores `new_value` as a
   string. ComfyUI coerces INT/FLOAT at validation, but `bool("false")` is `True`,
   so booleans are unsafe through that tool. Fixed in PR E.

6. `debug_agent.py` relies on star imports for `json`, `Dict`, `Any`,
   `function_tool`; `test_debug()` at the bottom calls `debug_workflow_errors`
   with the wrong arity (dead code).

## 1. `backend/tools/` package

One module per capability. Each exposes a plain async/sync function (testable,
reusable by the Phase 7a MCP server) and a thin `@function_tool` wrapper that
reads session context and returns a JSON string. `@function_tool` objects are
not directly callable, so tests target the plain functions.

```
backend/tools/__init__.py       # docstring only; no side-effect imports
backend/tools/_common.py        # tool_json(data) -> str; session_workflow() -> (session_id, workflow) | error dict
backend/tools/run_workflow.py   # PR D: validate/execute + watch
backend/tools/system.py         # PR D: get_system_stats
backend/tools/runtime_errors.py # PR E: classify_runtime_error, analyze_error
backend/tools/graph_edit.py     # PR E: replace_node_class
```

Import hygiene: `backend/tools/*` must not import `server`, `nodes`,
`execution`, or `folder_paths` at module scope. `backend/utils/modelscope_gateway.py`
imports `folder_paths` at top level; move it inside the method that uses it
(same precedent as `state_root()` in `service.py`). Add explicit
`import json` / `from typing import Dict, Any` / `from agents.tool import function_tool`
to `debug_agent.py` (PR D).

## 2. Lock redesign in `backend/llm/service.py` (PR B)

Serialize credential reads/writes and login per connection; allow concurrent
completions. Change in `events()` only, plus wrapping the credential write.
`connection_lock` / `connection_locks` keep their names.

```python
async def events(self, connection_id, payload, signin=None):
    guard = self.connection_lock(connection_id)
    while not guard.acquire(blocking=False):
        await asyncio.sleep(0.05)
    held = True                      # auth phase: credential read, spawn, any refresh
    process = None
    stderr_task = None
    try:
        command, request = self._request(connection_id, payload)
        process = await asyncio.create_subprocess_exec(...)          # unchanged
        ...                                                          # stderr drain unchanged
        result_seen = False
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), 920 if signin is not None else 330)
            if not line:
                break
            event = json.loads(line)
            if event['type'] == 'credential':
                with self.lock:
                    self.credentials.write(connection_id, event['value'])
                continue
            if held and signin is None:
                # provider.mjs delivers refreshes before any output; the rest of the stream is lock-free.
                guard.release()
                held = False
            if event['type'] == 'error':
                raise ValueError(event['message'])
            if event['type'] == 'result':
                result_seen = True
            yield event
        ...                                                          # exit-without-result handling unchanged
    finally:
        ...                                                          # kill process, cancel drain unchanged
        if held:
            guard.release()
```

- Login (`signin is not None`) holds the lock for the whole process, as today.
- `save_connection` keeps `with self.connection_lock(connection_id):` around
  its credential write.
- `self.lock` (existing RLock) also wraps the credential write inside `events`.
- Accepted risk: a provider that refreshes mid-stream would race a second
  stream started with the pre-refresh token. `provider.mjs` refreshes before
  output; document in a comment.

Existing tests: all unchanged in behaviour (`test_refresh_saved_before_provider_error`,
`test_cancellation_releases_connection`, `test_runtime_crash_logs_redacted_stderr`
still pass by analysis).

New tests in `TransportTests`:
- `test_concurrent_completions_on_one_connection`: fixture `slow.mjs` prints a
  `delta`, sleeps ~600 ms, prints `result`. `asyncio.gather(call(), call())`
  must finish in under ~1.0 s (serialized would be >= 1.2 s). Lock not held afterwards.
- `test_auth_phase_is_serialized`: fixture sleeps 300 ms then emits `credential`
  then `result`. Two concurrent calls take >= 600 ms; both credentials written;
  final stored credential is the last one.

`backend/llm/model.py`: no change.

## 3. Target concept in `backend/utils/comfy_gateway.py` (PR A)

```python
@dataclass(frozen=True)
class ComfyTarget:
    base_url: str                              # 'http://127.0.0.1:8188'
    headers: Mapping[str, str] = field(default_factory=dict)   # auth for a proxied pod (Phase 7b)
    name: str = 'local'

_target: contextvars.ContextVar[Optional[ComfyTarget]] = contextvars.ContextVar('comfy_target', default=None)

def local_target() -> ComfyTarget:
    # COPILOT_COMFY_URL env override first; otherwise derive from
    # server.PromptServer.instance.address/port with 0.0.0.0/:: mapped to 127.0.0.1. `import server` inside.
def get_target() -> ComfyTarget:      # _target.get() or local_target()
def set_target(target: Optional[ComfyTarget]) -> contextvars.Token
```

`ComfyGateway.__init__(self, target: Optional[ComfyTarget] = None, base_url: Optional[str] = None)`:
`base_url` kept for the existing standalone helpers, mapped to `ComfyTarget(base_url)`.
Remove the top-level `import nodes, execution, folder_paths, server` and the
unused `self.server_instance`. One private helper collapses the six copies of
session/timeout/error handling:

```python
async def _json(self, method: str, path: str, *, json_body=None, timeout: float = 30) -> tuple[int, Any]
    # returns (status, parsed body or {}); raises ComfyUnreachable on aiohttp.ClientConnectionError / asyncio.TimeoutError
```

Public methods keep names and return shapes (`run_prompt`, `get_object_info`,
`get_installed_nodes`, `manage_queue`, `interrupt_processing`, `get_history`,
`get_queue_status`). The existing `except aiohttp.ClientTimeout` clauses are
dead (not an exception class); the helper catches `asyncio.TimeoutError`.

New methods:

```python
async def validate_prompt(self, prompt: dict) -> dict
    # POST /api/copilot/validate -> {'supported': True, 'valid', 'error', 'node_errors', 'outputs'}; 404 -> {'supported': False}
async def get_system_stats(self) -> dict                       # GET /api/system_stats
async def cancel_prompt(self, prompt_id: str) -> None          # POST /api/queue {'delete':[id]} then POST /api/interrupt {'prompt_id': id}
async def wait_for_prompt(self, prompt_id: str, timeout: float, poll: float = 1.0) -> dict
    # loop: GET /api/history/{id}; if present return entry.
    #       GET /api/queue; if id in neither running nor pending: re-check history once, then raise PromptLost
    #       elapsed > timeout: raise PromptTimeout
class PromptTimeout(Exception); class PromptLost(Exception); class ComfyUnreachable(Exception)
```

The re-check after the queue poll closes the race in `task_done`, which pops
from `currently_running` and writes history under one mutex but is observed by
us through two HTTP calls.

## 4. `run_workflow` redesign (PR D)

### New route: `backend/controller/validate_api.py`

```python
@server.PromptServer.instance.routes.post("/api/copilot/validate")
async def validate_only(request):
    prompt = copy.deepcopy((await request.json())["prompt"])
    instance = server.PromptServer.instance
    if hasattr(instance, "node_replace_manager"):          # older ComfyUI lacks it
        instance.node_replace_manager.apply_replacements(prompt)
    valid, error, outputs, node_errors = await execution.validate_prompt(str(uuid.uuid4()), prompt, None)
    return web.json_response({"valid": valid, "error": error, "node_errors": node_errors, "outputs": outputs})
```

Register in the root `__init__.py` like the other controllers. Uncertainty:
`validate_prompt`'s three-argument signature is what 0.37.0 has; older
ComfyUI had two. Pin to the installed version.

### `backend/tools/run_workflow.py`

```python
EXECUTE_TIMEOUT = 600
TRACEBACK_TAIL = 15

async def check_workflow(gateway: ComfyGateway, prompt: dict, mode: str, timeout: float = EXECUTE_TIMEOUT,
                         client_id: Optional[str] = None) -> dict
```

- `mode == "validate"`: `gateway.validate_prompt(prompt)`. Returns
  `{"mode": "validate", "status": "valid" | "validation_failed" | "unsupported", "success": bool, "error", "node_errors", "target"}`.
  `unsupported` carries `"reason": "validate-only needs the Copilot node on the target; use mode='execute'"`.
- `mode == "execute"`: `gateway.run_prompt({"prompt": prompt, "client_id": client_id or f"copilot_{uuid}"})`.
  HTTP 400 -> `status: "validation_failed"` with `error`/`node_errors`.
  200 -> `wait_for_prompt(prompt_id, timeout)`; on `PromptTimeout` -> `cancel_prompt`, `status: "timeout"`;
  on `PromptLost` -> `"cancelled"`; on `asyncio.CancelledError` -> `cancel_prompt` then re-raise;
  on `ComfyUnreachable` -> `"unreachable"`.
- History entry -> `summarize_history(entry)`:
  - `status.status_str == 'success'` -> `"success"`, `outputs` = `entry["outputs"]` with each node's lists capped at 4 items and `meta` dropped.
  - message `execution_interrupted` present -> `"interrupted"`.
  - message `execution_error` present -> `"execution_error"`,
    `execution_error = {node_id, node_type, exception_type, exception_message, traceback_tail: traceback[-15:], executed}`;
    `current_inputs`/`current_outputs` dropped.
- Always includes `"success": status in ("valid", "success")` for the existing
  `analyze_error_type` text matcher, `prompt_id`, `elapsed_seconds`, `target`.

```python
@function_tool
async def run_workflow(mode: str = "validate", timeout_seconds: int = EXECUTE_TIMEOUT) -> str:
    """Check the session workflow against ComfyUI.
    mode="validate": structural check, nothing is queued, no GPU use.
    mode="execute": queues a real run on the GPU and waits for it; returns runtime errors with node id, type and traceback."""
```

Reads session/workflow via `_common.session_workflow()`, optional `client_id`
from `get_config().get("client_id")`.

### `backend/tools/system.py`

`get_system_stats()` -> trimmed `{"comfyui_version", "pytorch_version", "argv", "devices": [{"name", "vram_total_gb", "vram_free_gb"}]}`.
`argv` lets the runtime agent see whether `--lowvram` is already on.

## 5. Debug coordinator, completion rule, Runtime Error specialist (PR E)

### `backend/tools/runtime_errors.py`

```python
def classify_runtime_error(exception_type: str, message: str) -> str
    # 'oom' (OutOfMemoryError, "out of memory", "Allocation on device"), 'dtype' ("cutlass", "K mismatch",
    # "expected scalar type", "Half", "BFloat16", "mat1 and mat2 shapes"), 'shape' ("size mismatch",
    # "Sizes of tensors must match"), 'missing_file' (FileNotFoundError, "No such file"), 'other'
def analyze_error(error_text: str) -> dict
    # existing keyword logic from debug_agent.analyze_error_type moved here, plus: if the JSON has
    # "execution_error" -> error_type "runtime_<class>", recommended_agent "runtime_error_agent", affected_nodes [node_id]
```

`debug_agent.analyze_error_type` becomes a wrapper around `analyze_error`.

### `backend/tools/graph_edit.py`

```python
def replace_node_class_in(workflow: dict, object_info: dict, node_id: str, new_class: str, inputs: dict) -> dict
    # keeps links/values whose input name exists on new_class, applies `inputs`, returns changes summary
@function_tool
async def replace_node_class(node_id: str, new_class_type: str, inputs_json: str = "{}") -> str
    # saves via save_workflow_data, returns {"success", "changes", "ext": [workflow_update]}
```

Purpose: `VAEDecode` -> `VAEDecodeTiled`, `CheckpointLoaderSimple` -> `UNETLoader` fp8 / `UnetLoaderGGUF`.

`parameter_tools.update_workflow_parameter`: parse `new_value` with `json.loads`
when it succeeds (numbers, booleans), else keep the string.

### `debug_agent.py`

- New tool `report_limitation(reason: str, next_steps: str) -> str` returns
  `{"limitation": {...}}`; the coordinator must call it whenever it stops
  without a successful run.
- Coordinator prompt (replaces the "internal functions, not actual execution" note):

```
1. run_workflow(mode="validate"). On node_errors: analyze_error_type, hand off (Link / Parameter / Bugfix). Re-validate after every return.
2. When validation passes, run_workflow(mode="execute"). Say so before calling it: this queues a real run on the user's GPU.
3. status "execution_error": analyze_error_type; hand off to Runtime Error Agent for oom/dtype/shape, Bugfix Agent otherwise. After return go to 1.
4. Complete ONLY when an execute run returned status "success". Report what was changed, elapsed time, outputs.
5. If a fix needs the user (model download, input image, launch flag) or after 4 execute runs, call report_limitation and stop. Never say the workflow is fixed after validation alone.
```

- `Runner.run_streamed(..., max_turns=60)`.
- Runtime Error Agent: `create_agent(name="Runtime Error Agent", tools=[get_current_workflow, get_node_info, search_node_local, get_model_files, get_system_stats, update_workflow_parameter, replace_node_class, update_workflow], handoffs=[agent])`.
  Instructions carry the 8 GB playbook, cheapest first, and must state every quality trade-off:
  - **OOM**: `batch_size` -> 1; `EmptyLatentImage`/`EmptySD3LatentImage` width/height -> <= 1024^2 for SDXL/Flux, <= 768^2 for video; `VAEDecode` -> `VAEDecodeTiled` (tile 512) via `replace_node_class`; loader weights -> fp8 variant if `get_model_files` shows one (`UNETLoader.weight_dtype = fp8_e4m3fn`; GGUF only if `UnetLoaderGGUF` exists per `search_node_local`); `--lowvram` / `--novram` cannot be applied by the agent -> `report_limitation` with the exact `run_nvidia_gpu.bat` edit, after checking `argv` from `get_system_stats`.
  - **dtype** (`cutlass_fp16_linear: K mismatch`, `expected scalar type`, `mat1 and mat2 shapes`): mismatched model family/text encoder (SD1.5 CLIP into SDXL, wrong `DualCLIPLoader.type`), or fp8 weights on a node expecting fp16. Check loaders with `get_node_info`/`get_model_files`, fix with `update_workflow_parameter`; if the only fix is a different model file, `report_limitation`.
  - **shape**: width/height to multiples of 64; ControlNet/reference image sizes matched to the latent.
  - Always transfer back to the coordinator.
- Outcome tracking in `debug_workflow_errors`: map `raw_item.call_id` -> tool name on `tool_call_item`; on `tool_call_output_item` for `run_workflow` append the parsed JSON to `runs`, for `report_limitation` store `limitation`. Final ext:
  `debug_complete.data = {"status": "completed", "outcome": "executed" | "limitation" | "unresolved", "runs": [...], "limitation": ..., "final_agent", "events", "total_events"}`
  where `executed` requires the last execute run to have `status == "success"`. Pure helper `debug_outcome(runs, limitation) -> str` for tests.
- Remove `test_debug()` / `__main__` block.

## 6. Test harness: `tests/fake_comfy.py` (PR A)

```python
CUTLASS_ERROR = {"exception_type": "RuntimeError", "exception_message": "cutlass_fp16_linear: K mismatch (expected 2048, got 768)\n", "traceback": [...20 lines...]}

class FakeComfy:
    """aiohttp double for the ComfyUI endpoints the gateway uses."""
    def __init__(self, *, node_errors=None, execution='success', run_seconds=0.05, validate_route=True,
                 error=CUTLASS_ERROR, error_node=('9', 'KSampler'))
        # node_errors: dict -> /api/prompt and /api/copilot/validate report validation failure (400 / valid False)
        # execution: 'success' | 'error' | 'interrupted' | 'hang'
    async def start(self) -> str   # base_url on 127.0.0.1:0
    async def stop(self)
    prompts: list[dict]; interrupts: list[str]; deleted: list[str]; history: dict; pending/running sets
```

Routes: `POST /api/prompt` (validates by script, mints uuid, schedules `_run`
task), `GET /api/history/{id}`, `GET /api/queue`, `POST /api/queue`,
`POST /api/interrupt` (moves a running `hang` to history with
`execution_interrupted`), `GET /api/object_info[/{cls}]` (small canned set:
KSampler, VAEDecode, VAEDecodeTiled, EmptyLatentImage), `GET /api/system_stats`
(one 8 GB device), `POST /api/copilot/validate` when `validate_route`. `_run`
moves the id pending -> running -> history with `status.messages` shaped exactly
like ComfyUI (`execution_start`, then `execution_error` with
node_id/node_type/executed/current_inputs, or `execution_success`), plus
`outputs` for success.

Test modules (unittest, `IsolatedAsyncioTestCase`, `asyncSetUp` starts the fake
and calls `set_target(ComfyTarget(base_url, name='fake'))`):
- `tests/test_comfy_gateway.py` (PR A): target precedence (contextvar > env > `server` stub via `patch.dict(sys.modules, {'server': fake})`); `run_prompt` 200/400/unreachable; `wait_for_prompt` success, error, lost, timeout; `cancel_prompt` hits both endpoints; `validate_prompt` supported/404.
- `tests/test_run_workflow.py` (PR D): `check_workflow` for the four behaviours; timeout -> `interrupts == [prompt_id]` and `status == "timeout"`; `success` compat field; validate mode with and without the route; `summarize_history` traceback tail length.
- `tests/test_debug_support.py` (PR E): `classify_runtime_error` table; `analyze_error` on an execute result recommends `runtime_error_agent`; `debug_outcome`; `replace_node_class_in` keeps compatible links and drops incompatible ones; `update_workflow_parameter` value parsing (boolean case).
- `tests/test_llm_service.py` (PR B): two tests from section 2.

## 7. PR sequence

| # | PR | Phase | Files | Depends on |
|---|----|-------|-------|------------|
| A | Gateway target, HTTP helper, watch/cancel/validate/system-stats methods, `backend/tools/` package skeleton, fake ComfyUI double | 0 | `backend/utils/comfy_gateway.py`, `backend/utils/modelscope_gateway.py` (lazy `folder_paths`), `backend/tools/{__init__,_common}.py`, `tests/fake_comfy.py`, `tests/test_comfy_gateway.py` | — |
| B | Auth-phase lock: concurrent completions per connection | 0 | `backend/llm/service.py`, `tests/test_llm_service.py` | — |
| D | `run_workflow` validate/execute with runtime error capture; validate route; system stats tool; coordinator validates then executes; explicit imports in `debug_agent.py` | 1 | `backend/controller/validate_api.py`, `__init__.py`, `backend/tools/run_workflow.py`, `backend/tools/system.py`, `backend/service/debug_agent.py`, `tests/test_run_workflow.py` | A |
| E | Runtime Error Agent, `report_limitation`, execution-gated completion, error classification, `replace_node_class`, parameter value parsing, dead-code removal | 1 | `backend/tools/{runtime_errors,graph_edit}.py`, `backend/service/parameter_tools.py`, `backend/service/debug_agent.py`, `tests/test_debug_support.py` | D |
| F (optional) | UI: send `api.clientId` with debug requests; show `outcome`/`runs` in `DebugResult.tsx`; rebuild `dist/` | 1 | `ui/src/apis/workflowChatApi.ts`, `ui/src/components/chat/messages/DebugResult.tsx` | D |

(Original PR C — moving the three tool modules — deferred to Phase 10.)

## Flagged uncertainties

- ComfyUI version coupling: `validate_prompt` 3-arg signature, `node_replace_manager`, targeted `/interrupt` are all present in 0.37.0; older pods may differ. The validate route only exists where the Copilot node is installed; remote targets without it get `status: "unsupported"` and must use execute.
- Mid-stream credential refresh is assumed impossible (true for the current `provider.mjs`); documented as the one accepted race.
- `max_turns=60` and the 4-execute-run budget are judgement calls; adjust after the first real sessions.
