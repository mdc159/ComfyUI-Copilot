# Owned LLM connections

This fork replaces Copilot's LLM credentials, provider transport, and model selection.
Its existing OpenAI Agents SDK, prompts, workflow tools, debugging, GenLab, and agent
handoffs remain in place. This phase adds no research or optimization loop.

Open **Copilot → gear → Models & connections**.

- **API credentials:** OpenAI, Anthropic, OpenRouter, Gemini, Groq, xAI, and Kimi
  presets; custom OpenAI-compatible endpoints. Existing environment variables take
  effect without entering a key. An explicitly saved key takes precedence and is
  encrypted with Windows DPAPI. Latchkey can inject the configured key name when
  it is absent from the process environment. Restart ComfyUI after changing its
  inherited environment variables.
- **Provider sign-ins:** ChatGPT, Claude, GitHub Copilot, Kimi, OpenRouter, and xAI
  flows supported by the pinned provider library. Click Sign in, follow the
  provider link/instructions, and complete any code prompt. Authentication and
  model entitlement depend on the provider/account. A catalog is not proof of
  access; use Test model. Subscription connections never fall back to an API key.
- **Local:** start LM Studio's server (`http://127.0.0.1:1234/v1`) or Ollama
  (`http://127.0.0.1:11434/v1`) and load/install your chosen model separately.
  Refresh models, select it, test it, and save defaults. Local servers can use an
  optional saved key. Tool use and vision depend on the loaded model/server.
- Chat and workflow/debug can use separate connections. Workflow/debug defaults
  to the saved chat connection/model. The chat toolbar can choose another model
  within the saved chat connection.

The model service is owned by this fork. It calls provider transports directly
through `@earendil-works/pi-ai` 0.85.1, the same provider library used by Workbench.
Node subprocesses communicate with Python over pipes. There is no Workbench
proxy server or modification to Workbench. Python's existing Agents SDK still
executes the tools. Node.js 22.19+ must be on the ComfyUI process PATH.

Nonsecret selections live in `ComfyUI/user/copilot-llm/connections.json`.
Saved credentials live in encrypted `credentials/*.bin` beside it, bound to the
Windows user. Tokens are refreshed server-side and persisted before model output.
Settings writes require the loopback, same-origin settings interface. Credentials
are not returned to the browser or copied from existing CLI sign-ins. Removing a
saved credential leaves the environment variable source available for API entries.

The installed baseline's `COPILOT_REMOTE_TOOLS=false` setting is preserved because
upstream hosted MCP services were unavailable. Hosted-only knowledge/search
features are not reimplemented in this phase. Their availability is separate from
LLM authentication. Existing hosted intent routes are likewise not redesigned.

## Install on another Windows portable ComfyUI

Use this fork's `main` branch. Install Git and Node.js 22.19 or newer and make
`node` and `npm` available on PATH before starting ComfyUI. Saved API keys and
subscription credentials currently require Windows; they cannot be copied to
another Windows user or machine. Latchkey is optional when using saved keys,
environment keys, subscription sign-ins, or unauthenticated local servers.

Stop ComfyUI first. If Copilot is already installed, move its entire custom-node
folder outside `custom_nodes` as a backup. Keep only one Copilot installation.
In PowerShell, change `$portable` to the destination installation:

```powershell
$ErrorActionPreference = 'Stop'
$portable = 'D:\ComfyUI_windows_portable'
$node = Join-Path $portable 'ComfyUI\custom_nodes\ComfyUI-Copilot'
$python = Join-Path $portable 'python_embeded\python.exe'
node --version
git clone --branch main https://github.com/mdc159/ComfyUI-Copilot.git $node
if ($LASTEXITCODE -ne 0) { throw 'Clone failed; check for an existing Copilot folder' }
& $python -m pip install -r (Join-Path $node 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed' }
npm.cmd --prefix (Join-Path $node 'llm-runtime') ci --omit=dev --ignore-scripts
if ($LASTEXITCODE -ne 0) { throw 'Node dependency installation failed' }
if (-not (Test-Path -LiteralPath (Join-Path $node '.env'))) {
    Copy-Item -LiteralPath (Join-Path $node '.env.example') -Destination (Join-Path $node '.env')
}
```

The production UI is included; no frontend build is needed. Start ComfyUI through
your usual launcher and open `http://127.0.0.1:8188/` on that machine. Settings
are restricted to local access. In **Copilot → gear → Models & connections**,
choose a connection, add a key or complete sign-in, **Refresh models**, select a
model, **Test model**, and **Save defaults**. For passkeys, open the provider's
sign-in link in your regular browser with the password-manager extension enabled.
LM Studio/Ollama must be running on the ComfyUI machine for the default URLs.

The example `.env` disables unavailable upstream hosted MCP tools. Review an
existing `.env` when upgrading; do not overwrite it or copy credentials into Git.
To update a clean installation, stop ComfyUI, run `git -C $node pull --ff-only`,
then repeat the Python and Node dependency commands above and restart. The fork
is installed from GitHub; the upstream Comfy registry entry is not this fork.

## Development and deployment from a separate checkout

```powershell
cd D:/Projects/ComfyUI-Copilot/llm-runtime
npm ci --omit=dev --ignore-scripts
cd ../ui
npm ci
npm run build
cd ..
D:/ComfyUI_windows_portable/python_embeded/python.exe -m unittest discover -s tests -v
./scripts/install-standalone.ps1
```

The installer stages a copy, preserves node `.env` and runtime data, verifies an
idle image queue, copies the previous installation into `copilot-backups`, and
installs the staged copy. Windows directory handles may prevent a rename, so the
installer verifies a complete backup before copying files. This helper is for
deploying a separate development checkout to an existing, running standalone
installation; it does not install Python requirements. Use the fresh-install
instructions above for another machine. Restart through ComfyUI Manager afterwards.
Rollback: while ComfyUI is stopped, move the
current node aside, then restore the recorded backup to the custom-node path.

Regression tests cover DPAPI, no subscription/API fallback, configuration
persistence, local model discovery and streaming, multi-turn tool execution,
structured output, OAuth interaction/refresh fixtures, cancellation, and the
settings origin boundary. Live cloud verification covers OpenAI API and ChatGPT
subscription sign-in with a successful `gpt-5.6-sol` model test. Account entitlements
vary; one other catalog model was rejected by the provider, so test your selection.
Other subscription providers and actual loaded LM Studio/Ollama inference remain
unverified; local protocol behavior is covered by fixtures. The upstream global TypeScript check is
blocked by pre-existing JSX syntax in `markdownComponents.ts`; the new settings
component has a separate passing typecheck and the production build succeeds.
