"""Classify workflow errors so the debug coordinator can route them to a specialist.

``classify_runtime_error`` buckets an execution error; ``analyze_error`` is the keyword
matcher the coordinator's ``analyze_error_type`` tool wraps.
"""
import json
import re
from typing import Any, Dict, List, Optional

# Ordered: the first class whose keywords match wins. Matching is case-insensitive
# on the exception type and message together.
RUNTIME_ERROR_KEYWORDS = (
    ("oom", ("outofmemoryerror", "out of memory", "allocation on device")),
    ("dtype", ("cutlass", "k mismatch", "expected scalar type", "half", "bfloat16", "mat1 and mat2 shapes")),
    ("shape", ("size mismatch", "sizes of tensors must match")),
    ("missing_file", ("filenotfounderror", "no such file")),
)

SUCCESS_MARKERS = ('"success": true', "'success': true", "validation successful", "workflow validation successful")
CONNECTION_KEYWORDS = [
    "connection", "input connection", "required input", "missing input",
    "not connected", "no connection", "link", "output", "socket",
    "missing_input", "invalid_connection", "connection_error",
]
PARAMETER_KEYWORDS = [
    "value not in list", "invalid value", "not found in list",
    "parameter value", "invalid parameter", "model not found",
    "invalid image file", "value_not_in_list", "invalid_input",
]
GENERAL_ERROR_KEYWORDS = ["error", "failed", "exception", "invalid"]


def classify_runtime_error(exception_type: str, message: str) -> str:
    """'oom' | 'dtype' | 'shape' | 'missing_file' | 'other' for an execution error."""
    text = f"{exception_type or ''} {message or ''}".lower()
    for error_class, keywords in RUNTIME_ERROR_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return error_class
    return "other"


def _execution_error(error_data: Any) -> Optional[Dict[str, Any]]:
    """The execution_error dict inside a run_workflow result, if error_data carries one."""
    data = error_data
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(data, dict) and isinstance(data.get("execution_error"), dict):
        return data["execution_error"]
    return None


def analyze_error(error_data: Any) -> Dict[str, Any]:
    """Keyword analysis of a validation or execution result; JSON string, dict or plain text."""
    analysis: Dict[str, Any] = {
        "error_type": "unknown",
        "recommended_agent": "workflow_bugfix_default_agent",
        "error_details": [],
        "affected_nodes": [],
    }

    execution_error = _execution_error(error_data)
    if execution_error is not None:
        error_class = classify_runtime_error(execution_error.get("exception_type") or "",
                                             execution_error.get("exception_message") or "")
        analysis["error_type"] = f"runtime_{error_class}"
        analysis["error_class"] = error_class
        analysis["recommended_agent"] = "runtime_error_agent"
        node_id = execution_error.get("node_id")
        analysis["affected_nodes"] = [str(node_id)] if node_id is not None else []
        analysis["error_details"] = [{
            "error_type": f"runtime_{error_class}",
            "node_id": node_id,
            "node_type": execution_error.get("node_type"),
            "exception_type": execution_error.get("exception_type"),
            "message": execution_error.get("exception_message"),
        }]
        return analysis

    error_text = str(error_data).lower()

    if any(marker in error_text for marker in SUCCESS_MARKERS):
        analysis["error_type"] = "no_error"
        analysis["recommended_agent"] = "none"
        analysis["error_details"] = [{"message": "Workflow validation successful"}]
        return analysis

    node_id_matches = re.findall(r'"(\d+)":', error_text) or re.findall(r"'(\d+)':", error_text)
    if node_id_matches:
        analysis["affected_nodes"] = list(set(node_id_matches))

    connection_errors = sum(error_text.count(keyword) for keyword in CONNECTION_KEYWORDS)
    parameter_errors = sum(error_text.count(keyword) for keyword in PARAMETER_KEYWORDS)
    other_errors = 0
    if connection_errors == 0 and parameter_errors == 0:
        if any(keyword in error_text for keyword in GENERAL_ERROR_KEYWORDS):
            other_errors = 1

    if connection_errors > 0 and parameter_errors == 0 and other_errors == 0:
        analysis["error_type"] = "connection_error"
        analysis["recommended_agent"] = "link_agent"
    elif connection_errors > 0:
        analysis["error_type"] = "mixed_connection_error"
        analysis["recommended_agent"] = "link_agent"
    elif parameter_errors > 0:
        analysis["error_type"] = "parameter_error"
        analysis["recommended_agent"] = "parameter_agent"
    elif other_errors > 0:
        analysis["error_type"] = "structural_error"
        analysis["recommended_agent"] = "workflow_bugfix_default_agent"

    if connection_errors > 0:
        analysis["error_details"].append({
            "error_type": "connection_error",
            "message": f"Detected {connection_errors} connection-related issues",
            "details": "Connection or input/output related errors found",
        })
    if parameter_errors > 0:
        analysis["error_details"].append({
            "error_type": "parameter_error",
            "message": f"Detected {parameter_errors} parameter-related issues",
            "details": "Parameter value or configuration related errors found",
        })
    return analysis
