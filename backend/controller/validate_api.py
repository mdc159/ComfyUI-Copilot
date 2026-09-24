"""Validate-only route: runs ComfyUI's prompt validation without queueing anything.

Mirrors the validation half of PromptServer.post_prompt (server.py) so the gateway
can check a workflow over HTTP against any target that has this node installed.
"""
import copy
import uuid

import execution
import server
from aiohttp import web


@server.PromptServer.instance.routes.post("/api/copilot/validate")
async def validate_only(request):
    body = await request.json()
    prompt = body.get("prompt") if isinstance(body, dict) else None
    if not isinstance(prompt, dict):
        return web.json_response({"error": "body must be {\"prompt\": {...}}"}, status=400)
    prompt = copy.deepcopy(prompt)   # validate_prompt mutates the prompt it is given
    instance = server.PromptServer.instance
    if hasattr(instance, "node_replace_manager"):          # older ComfyUI lacks it
        instance.node_replace_manager.apply_replacements(prompt)
    valid, error, outputs, node_errors = await execution.validate_prompt(str(uuid.uuid4()), prompt, None)
    return web.json_response({"valid": valid, "error": error, "node_errors": node_errors, "outputs": outputs})
