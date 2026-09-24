"""Helpers shared by the tool modules."""
import json
from typing import Any, Dict, Tuple, Union

from ..utils.request_context import get_session_id


def tool_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def session_workflow() -> Union[Tuple[str, Dict[str, Any]], Dict[str, str]]:
    """Session id and workflow for the current request, or the error dict the tool should return."""
    session_id = get_session_id()
    if not session_id:
        return {"error": "No session_id found in context"}
    # Importing the DAO opens the sqlite file; keep that out of module import.
    from ..dao.workflow_table import get_workflow_data
    workflow = get_workflow_data(session_id)
    if not workflow:
        return {"error": "No workflow data found for this session"}
    return session_id, workflow
