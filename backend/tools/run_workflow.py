"""Validate or execute the session workflow against the current ComfyUI target.

``check_workflow`` / ``summarize_history`` are plain functions; ``run_workflow`` is the
agent tool built on them.
"""
import asyncio
import time
import uuid
from typing import Any, Dict, Optional

from agents.tool import function_tool

from ..utils.comfy_gateway import ComfyGateway, ComfyUnreachable, PromptLost, PromptTimeout
from ..utils.logger import log
from ..utils.request_context import get_config
from ._common import session_workflow, tool_json

EXECUTE_TIMEOUT = 600
TRACEBACK_TAIL = 15
MAX_OUTPUT_ITEMS = 4
MODES = ("validate", "execute")
UNSUPPORTED_REASON = "validate-only needs the Copilot node on the target; use mode='execute'"


def _trim_outputs(outputs: Dict[str, Any]) -> Dict[str, Any]:
    """Each node's output lists capped at MAX_OUTPUT_ITEMS; non-list values pass through."""
    trimmed = {}
    for node_id, node_outputs in (outputs or {}).items():
        if isinstance(node_outputs, dict):
            trimmed[node_id] = {key: value[:MAX_OUTPUT_ITEMS] if isinstance(value, list) else value
                                for key, value in node_outputs.items()}
        else:
            trimmed[node_id] = node_outputs
    return trimmed


def summarize_history(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a /history entry to status plus the fields an agent needs; `meta` is dropped."""
    status = entry.get("status") or {}
    messages = [m for m in status.get("messages") or [] if isinstance(m, (list, tuple)) and len(m) == 2]
    events = {event: data for event, data in messages}
    if status.get("status_str") == "success":
        return {"status": "success", "outputs": _trim_outputs(entry.get("outputs") or {})}
    if "execution_error" in events:
        data = events["execution_error"] or {}
        traceback = data.get("traceback") or []
        return {"status": "execution_error", "execution_error": {
            "node_id": data.get("node_id"),
            "node_type": data.get("node_type"),
            "exception_type": data.get("exception_type"),
            "exception_message": data.get("exception_message"),
            "traceback_tail": list(traceback[-TRACEBACK_TAIL:]),
            "executed": data.get("executed", []),
        }}
    if "execution_interrupted" in events:
        data = events["execution_interrupted"] or {}
        return {"status": "interrupted", "node_id": data.get("node_id"), "node_type": data.get("node_type"),
                "executed": data.get("executed", [])}
    return {"status": "execution_error", "execution_error": None,
            "error": f"history status '{status.get('status_str')}' without an execution_error or execution_interrupted message"}


async def check_workflow(gateway: ComfyGateway, prompt: Dict[str, Any], mode: str, timeout: float = EXECUTE_TIMEOUT,
                         client_id: Optional[str] = None, poll: float = 1.0) -> Dict[str, Any]:
    """Validate (nothing queued) or execute (queued and waited for) `prompt` on `gateway`'s target."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    started = time.monotonic()
    result: Dict[str, Any] = {"mode": mode, "target": gateway.target.name, "prompt_id": None}

    def finish(**fields) -> Dict[str, Any]:
        result.update(fields)
        result["success"] = result.get("status") in ("valid", "success")
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        return result

    if mode == "validate":
        try:
            reply = await gateway.validate_prompt(prompt)
        except ComfyUnreachable as e:
            return finish(status="unreachable", error=str(e))
        if not reply.get("supported"):
            return finish(status="unsupported", reason=UNSUPPORTED_REASON)
        if reply.get("valid"):
            return finish(status="valid", error=None, node_errors={}, outputs=reply.get("outputs", []))
        return finish(status="validation_failed", error=reply.get("error"), node_errors=reply.get("node_errors", {}))

    reply = await gateway.run_prompt({"prompt": prompt, "client_id": client_id or f"copilot_{uuid.uuid4()}"})
    if not reply.get("success"):
        error = reply.get("error")
        if isinstance(error, dict) and error.get("type") == "connection_error":
            return finish(status="unreachable", error=error)
        return finish(status="validation_failed", error=error, node_errors=reply.get("node_errors", {}))
    prompt_id = reply.get("prompt_id")
    result["prompt_id"] = prompt_id
    try:
        entry = await gateway.wait_for_prompt(prompt_id, timeout, poll=poll)
    except PromptTimeout:
        await gateway.cancel_prompt(prompt_id)
        return finish(status="timeout", error=f"no result after {timeout} seconds; the run was cancelled")
    except PromptLost:
        return finish(status="cancelled", error="the prompt left the queue without a history entry")
    except asyncio.CancelledError:
        await gateway.cancel_prompt(prompt_id)
        raise
    except ComfyUnreachable as e:
        return finish(status="unreachable", error=str(e))
    return finish(**summarize_history(entry))


@function_tool
async def run_workflow(mode: str = "validate", timeout_seconds: int = EXECUTE_TIMEOUT) -> str:
    """Check the session workflow against ComfyUI.
    mode="validate": structural check, nothing is queued, no GPU use.
    mode="execute": queues a real run on the GPU and waits for it; returns runtime errors with node id, type and traceback."""
    if mode not in MODES:
        return tool_json({"error": f"mode must be 'validate' or 'execute', got {mode!r}"})
    found = session_workflow()
    if isinstance(found, dict):
        return tool_json(found)
    session_id, workflow = found
    client_id = (get_config() or {}).get("client_id")
    log.info(f"run_workflow mode={mode} session={session_id}")
    try:
        result = await check_workflow(ComfyGateway(), workflow, mode, timeout_seconds, client_id)
    except Exception as e:
        log.error(f"run_workflow failed: {e}")
        return tool_json({"error": f"Failed to run workflow: {e}", "mode": mode, "success": False})
    log.info(f"run_workflow mode={mode} status={result.get('status')} prompt_id={result.get('prompt_id')}")
    return tool_json(result)
