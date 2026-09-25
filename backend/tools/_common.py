"""Helpers shared by the tool modules."""
import json
from typing import Any, Dict, Tuple, Union

from ..utils.request_context import get_session_id, get_config


def tool_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def session_workflow() -> Union[Tuple[str, Dict[str, Any]], Dict[str, str]]:
    """Session id and workflow for the current request, or the error dict the tool should return.

    Resolves through the pinned checkpoint / workflow_key (see workflow_rewrite_tools.
    get_workflow_data_from_config) rather than "latest row for this session", so a tool never
    picks up a newer version saved by another browser tab mid-run.
    """
    session_id = get_session_id()
    if not session_id:
        return {"error": "No session_id found in context"}
    # Importing these lazily keeps the sqlite file / agents package out of module import.
    from ..service.workflow_rewrite_tools import get_workflow_data_from_config
    config = get_config() or {"session_id": session_id}
    workflow = get_workflow_data_from_config(config)
    if not workflow:
        return {"error": "No workflow data found for this session"}
    return session_id, workflow
