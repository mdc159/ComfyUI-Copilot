# The mdc159 fork is the maintained line

**Date:** 2026-09-24
**Decided by:** Mike

> "I believe the original is abandoned now. There won't be any more updates to
> it so we pretty much own this one moving forward however you recommend we
> manage it."

Context: upstream (`ATH-MaaS/ComfyUI-Copilot`, formerly AIDC-AI) posted an API
service suspension notice. Its last push was 2026-09-11, and its hosted MCP
server was returning HTTP 503 on 2026-09-20.

## How the fork is managed

These are the agent's working defaults, adopted to keep moving. Change them freely.

- `mdc159/ComfyUI-Copilot` `main` is the source of truth. Changes go through
  a branch and pull request, then merge to `main`.
- Development happens in `D:\Projects\ComfyUI-Copilot`.
- Each ComfyUI installation is a plain clone of `origin/main` in
  `custom_nodes\ComfyUI-Copilot`. Deploy by stopping ComfyUI, running
  `git pull --ff-only`, installing any changed dependencies, and restarting.
  No copying, staging folders, or uncommitted overlays.
- Machine-local state stays untracked in the installed folder: `.env`,
  `backend/data/*.db`, and `backend/logs/`. The same applies to
  `ComfyUI/user/copilot-llm/`.
- Every merge adds a `CHANGELOG.md` entry with what changed, how it was
  verified, and where it was deployed. The version in `pyproject.toml` is
  bumped, and the merge commit is tagged `vX.Y.Z`.
- The `upstream` remote stays for reference only. Nothing is merged from it
  unless upstream unexpectedly resumes, in which case changes are pulled
  selectively.
- Decisions made by Mike are recorded here with his words and the date.
