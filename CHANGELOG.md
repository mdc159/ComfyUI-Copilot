# Changelog

This file records changes to the mdc159 fork and each deployment to a ComfyUI
installation. The upstream project (AIDC-AI / ATH-MaaS) is treated as finished;
see [decisions/2026-09-24-fork-is-canonical.md](decisions/2026-09-24-fork-is-canonical.md).

Format: newest first. Each release lists what changed, how it was verified, and
where it was deployed. Versions follow `pyproject.toml`; tags are `vX.Y.Z`.

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
