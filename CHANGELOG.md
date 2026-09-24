# Changelog

This file records changes to the mdc159 fork and each deployment to a ComfyUI
installation. The upstream project (AIDC-AI / ATH-MaaS) is treated as finished;
see [decisions/2026-09-24-fork-is-canonical.md](decisions/2026-09-24-fork-is-canonical.md).

Format: newest first. Each release lists what changed, how it was verified, and
where it was deployed. Versions follow `pyproject.toml`; tags are `vX.Y.Z`.

## Unreleased

Roadmap phase 0 (Foundation), the groundwork for rebuilding the features that
were lost with the upstream hosted server. See `ROADMAP.md` and
`docs/design/phase-0-1-debugger.md`. Nothing in this section changes what the
user sees yet.

### Changed
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
- `python_embeded\python.exe -m unittest discover -s tests`: 28 tests pass
  on `main` at d8fc95d.
- Developer note: a fresh worktree needs `npm ci` in `llm-runtime/` before
  the `test_llm_service` tests can start the Node provider process (see
  `LLM-CONNECTIONS.md`).

### Deployed
- Not yet.

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
- Model calls on one connection run one at a time. The workflow model uses
  the chat connection by default, so concurrent chat and debug runs wait for
  each other.
- The chat endpoints are not restricted to local requests. Do not start
  ComfyUI with `--listen` on an untrusted network; others could use the
  configured subscription.
- `pyproject.toml` still uses upstream's Comfy Registry publisher ID
  (`yx9966`). Do not update this node through ComfyUI-Manager's registry
  path; update with `git pull` instead.
