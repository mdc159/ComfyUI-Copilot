# Changelog

This file records changes to the mdc159 fork and each deployment to a ComfyUI
installation. The upstream project (AIDC-AI / ATH-MaaS) is treated as finished;
see [decisions/2026-09-24-fork-is-canonical.md](decisions/2026-09-24-fork-is-canonical.md).

Format: newest first. Each release lists what changed, how it was verified, and
where it was deployed. Versions follow `pyproject.toml`; tags are `vX.Y.Z`.

## 2.3.0 — 2026-09-24

### Changed
- Debug and rewrite runs are now bound to the workflow tab they started on. Before,
  Copilot identified "the workflow" as the newest one saved under a browser-wide
  session, so with several ComfyUI tabs open a debug run could read another tab's
  graph mid-run, and its fix could be written into whatever tab was active when it
  arrived. Now every request carries the tab's workflow identity, the run is pinned
  to the version saved at its start, and the UI refuses to apply a change to a
  different tab — it shows "This change belongs to workflow X — switch to that tab
  and click Apply" instead.
- The suggestion chips shown in an empty chat (for example "debug the workflow of
  the current canvas") used to replay a pre-recorded demo conversation and never
  contacted the backend; the recorded debug always ended "ready for execution". The
  recordings are deleted; the debug chip now runs the real debugger and the other
  chips send their text as a real message.

### Added
- Offline node index built from ComfyUI-Manager's node database (PR #9): a local
  join of the Manager's ~41,000 node classes across ~6,000 packs with the live
  list of installed nodes, supporting ranked keyword search. When Manager's
  database files exist on disk, searches run without network access; otherwise
  the database is fetched and cached for one week. The index builds once per
  process (2–15 seconds) and answers searches in ~5–30 ms. Chat integration
  shipped in PR #12 below.
- Chat agent node discovery tools (PR #12): the chat agent can now recommend
  nodes ("which node does X"), describe any node class ("what does node X do"),
  and validate a list of node classes against what's installed, all from the
  local node index with no upstream server call. A new `/api/copilot/node_info_by_types`
  route replaces the dead upstream call that the "Accept workflow" flow used for
  the missing-node install guide; the built frontend was rebuilt so it no longer
  calls the upstream host for that guidance. When the ModelScope web-search tool
  is off, the agent is told to say it found nothing rather than invent node names.

### Verified
- All 82 tests pass with the portable Python. The concurrency regression test
  now requires both provider streams to start before allowing either to finish,
  instead of comparing elapsed time. It passed 10 consecutive runs and rejects
  forced full-stream serialization. This maintenance change affects tests only.

### Deployed
- `D:\ComfyUI_windows_portable` on 2026-09-24.

## 2.2.0 — 2026-09-24

Roadmap phases 0 (Foundation) and 1 (Debugger sees real execution). Phase 0
is the groundwork for rebuilding the features that were lost with the
upstream hosted server; phase 1 is the first change the user sees: the debug
agent now runs the workflow for real and reads back what actually failed. See
`ROADMAP.md` and `docs/design/phase-0-1-debugger.md`.

### Changed
- The debugger now sees real execution (PRs #6, #7). Until now its
  `run_workflow` tool posted the workflow to ComfyUI's `/api/prompt`, which
  validates the graph and then queues a real run. The agent read the
  validation reply, reported the workflow fixed, and exited, while the run
  that followed failed on the GPU with an error the agent never saw. It now
  works in two steps. First it validates without touching the GPU
  (`mode="validate"`, through a new `/api/copilot/validate` route that checks
  the graph without queueing anything). When validation passes it runs the
  workflow for real (`mode="execute"`), waits for the run to finish, and reads
  back the actual result: on failure, the failing node, the exception, and
  the last 15 lines of the traceback; on success, the outputs. Runs that time
  out are cancelled and reported as such.
- "Fixed" is only reported after a real run has succeeded (PR #7). If the
  agent cannot get there, because the fix needs the user (a model download,
  an input image, a launch flag) or because four real runs have not
  succeeded, it reports a limitation with exact next steps instead, for
  example the line to change in `run_nvidia_gpu.bat` to add `--lowvram`. The
  debug summary carries the outcome (`executed`, `limitation`, or
  `unresolved`) and the list of runs.
- `update_workflow_parameter` now parses numbers and booleans (PR #7): a
  value of `"512"` is stored as the integer 512 and `"false"` as a boolean,
  where before both were stored as text.
- Chat and debug runs on the same model connection no longer wait for each
  other (PR #3). The per-connection lock used to be held for the whole model
  call; it is now held only while credentials are read, the provider process
  is started, and any token refresh is written, then released once the model
  starts producing output. Sign-in still holds the lock for its whole run.
  This closes the "Model calls on one connection run one at a time" entry
  under Known issues.
- The ComfyUI gateway (`backend/utils/comfy_gateway.py`) now talks to a named
  *target* (PR #4): a URL plus optional headers describing which ComfyUI
  instance the tools use. It defaults to the local server (or
  `COPILOT_COMFY_URL` if set) and is the hook that later lets the same tools
  drive a Runpod instance (roadmap phase 7b). The six copies of HTTP
  request/timeout/error handling were collapsed into one helper; existing
  gateway methods keep their names and return shapes. The gateway no longer
  imports ComfyUI's own modules at import time, so it can be tested outside
  ComfyUI.

### Added
- Runtime Error Agent (PR #7): a debug specialist for out-of-memory, dtype,
  and shape failures. It applies an 8 GB-VRAM playbook, cheapest change
  first: batch size to 1; resolution caps (at most 1024x1024 for SDXL and
  Flux, 768x768 for video); tiled VAE decode (`VAEDecode` ->
  `VAEDecodeTiled`); fp8 or GGUF weights when a matching model file or loader
  node is installed. It explains the quality trade-off of each change. For
  dtype errors (such as `cutlass_fp16_linear: K mismatch`) it looks for a
  mismatched model family or text encoder and fixes the loader setting; if
  the only fix is a different model file, or a `--lowvram` launch flag, it
  reports a limitation rather than pretending.
- `replace_node_class` tool (PR #7): swaps a node for another node type,
  keeping the links and values the new type accepts and dropping the rest.
- `run_workflow` tool with `mode="validate"` / `mode="execute"`,
  `get_system_stats` tool (ComfyUI version, launch arguments, VRAM per
  device), and the `/api/copilot/validate` route (PR #6);
  `report_limitation` tool and runtime error classification
  (`backend/tools/runtime_errors.py`) (PR #7).
- Tests `tests/test_run_workflow.py` (16 tests against the fake ComfyUI:
  validation failure, runtime failure, success, timeout, cancellation) and
  `tests/test_debug_support.py` (14 tests: error classification, node class
  replacement, parameter parsing, outcome rules) (PRs #6, #7).
- Gateway methods `validate_prompt`, `get_system_stats`, `cancel_prompt`, and
  `wait_for_prompt`, plus `ComfyUnreachable`, `PromptTimeout`, and
  `PromptLost` exceptions (PR #4). These let the phase 1 debugger watch a
  queued run until it finishes instead of stopping at validation.
- `backend/tools/` package skeleton for local tool implementations (PR #4).
- `tests/fake_comfy.py`: an in-process fake ComfyUI that answers the
  endpoints the gateway uses and can be told to fail validation, fail at
  runtime, succeed, or hang (PR #4). Phase 1 tests build on it.
- Regression tests `test_concurrent_completions_on_one_connection` and
  `test_auth_phase_is_serialized` (PR #3); `tests/test_comfy_gateway.py`
  covering target precedence, `run_prompt`, `wait_for_prompt`,
  `cancel_prompt`, and `validate_prompt` (PR #4).

### Verified
- `python_embeded\python.exe -m unittest discover -s tests`: 58 tests pass
  on `main` at 3166571 (28 after phase 0, 30 more from PRs #6 and #7).
- Developer note: a fresh worktree needs `npm ci` in `llm-runtime/` before
  the `test_llm_service` tests can start the Node provider process (see
  `LLM-CONNECTIONS.md`).

### Deployed
- `D:\ComfyUI_windows_portable` on 2026-09-24.

## 2.1.1 — 2026-09-24

### Changed
- Model runtime stderr is captured and drained. When the Node provider process
  exits without a result, its exit code and last 40 stderr lines are logged as a
  warning, with credential values redacted. Previously stderr was discarded.
- The "No valid Authorization header found" message is logged at debug level
  instead of error. It appeared on every chat and debug request because the
  hosted upstream service is not used.

### Added
- `CHANGELOG.md` and `decisions/` for maintaining the fork.
- Regression test `test_runtime_crash_logs_redacted_stderr`.

### Verified
- `python_embeded\python.exe -m unittest discover -s tests`: 13 tests pass.
  (Use unittest, not pytest: pytest imports the node's root `__init__.py`,
  which only works inside ComfyUI.)

### Deployed
- `D:\ComfyUI_windows_portable` on 2026-09-24. The installed folder was
  converted from an upstream checkout with an uncommitted overlay into a clean
  checkout of `origin/main`. Stale frontend bundles, the staging copy, and the
  partial 47-file backup were removed. Legacy `CC_OPENAI_*` and
  `WORKFLOW_LLM_*` entries were removed from the installed `.env`; they are no
  longer read.

## 2.1.0 — 2026-09-20 (commit d99c68b)

### Changed
- Replaced upstream's model authentication and selection with a
  fork-owned model service (`backend/llm/`, `llm-runtime/provider.mjs`).
  Upstream agents, prompts, and workflow tools are unchanged; they call models
  through `CopilotModel`, an OpenAI Agents SDK `Model` implementation.
- Connections: API keys (OpenAI, OpenRouter, Anthropic, Gemini, Groq, xAI,
  Kimi), provider subscription sign-ins, custom OpenAI-compatible endpoints,
  and local LM Studio / Ollama. Keys are encrypted with Windows DPAPI under
  `ComfyUI/user/copilot-llm/`; browsers never send or store model credentials.
- Settings endpoints accept only local requests and reject cross-origin writes.
- Upstream hosted MCP tools can be disabled with `COPILOT_REMOTE_TOOLS=false`
  (the hosted service was returning HTTP 503).
- Higher-contrast chat UI; rebuilt frontend in `dist/`.

### Verified
- 12 regression tests; live OpenAI API test and streamed chat on the portable
  install (recorded in `D:\ComfyUI_windows_portable\copilot-installation.json`).

### Deployed
- `D:\ComfyUI_windows_portable` on 2026-09-20, as an uncommitted overlay on an
  upstream checkout. Rollback copy:
  `copilot-backups\ComfyUI-Copilot-20260920-145132-complete`.

## Known issues
- The debugger's execute mode queues a real run; on the 4070 expect each
  debug cycle to take as long as the workflow itself.
- The chat endpoints are not restricted to local requests. Do not start
  ComfyUI with `--listen` on an untrusted network; others could use the
  configured subscription.
- `pyproject.toml` still uses upstream's Comfy Registry publisher ID
  (`yx9966`). Do not update this node through ComfyUI-Manager's registry
  path; update with `git pull` instead.
