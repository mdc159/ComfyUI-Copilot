"""Swap a node's class while keeping the links and values the new class accepts.

``replace_node_class_in`` is the pure graph edit; ``replace_node_class`` is the agent tool
that fetches object_info, saves the session workflow and returns a workflow_update ext.
Typical use: VAEDecode -> VAEDecodeTiled, CheckpointLoaderSimple -> UNETLoader.
"""
import copy
import json
from typing import Any, Dict, Optional

from agents.tool import function_tool

from ..utils.comfy_gateway import ComfyGateway
from ..utils.request_context import get_config
from ._common import session_workflow, tool_json


def _input_names(object_info: Dict[str, Any], class_type: str) -> Optional[set]:
    """Input names a class accepts per object_info, or None when the class is unknown."""
    node_def = object_info.get(class_type)
    if not isinstance(node_def, dict):
        return None
    names = set()
    for section in (node_def.get("input") or {}).values():
        if isinstance(section, dict):
            names.update(section.keys())
    return names


def replace_node_class_in(workflow: Dict[str, Any], object_info: Dict[str, Any], node_id: str,
                          new_class: str, inputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return {"workflow", "changes"} with node `node_id` re-typed to `new_class`.

    Existing inputs (links and values) are kept only when the new class declares an input of
    that name; `inputs` are applied on top. The input workflow is not mutated.
    """
    node_id = str(node_id)
    if node_id not in workflow:
        return {"error": f"Node {node_id} not found in workflow"}
    accepted = _input_names(object_info, new_class)
    if accepted is None:
        return {"error": f"Node class {new_class} not found in object_info"}

    updated = copy.deepcopy(workflow)
    node = updated[node_id]
    old_class = node.get("class_type")
    old_inputs = node.get("inputs") or {}
    kept, dropped = {}, []
    for name, value in old_inputs.items():
        if name in accepted:
            kept[name] = value
        else:
            dropped.append(name)
    applied = {}
    for name, value in (inputs or {}).items():
        if name not in accepted:
            return {"error": f"Input {name} does not exist on {new_class}"}
        kept[name] = value
        applied[name] = value

    node["class_type"] = new_class
    node["inputs"] = kept
    if isinstance(node.get("_meta"), dict) and node["_meta"].get("title") == old_class:
        node["_meta"]["title"] = new_class

    changes = {
        "node_id": node_id,
        "old_class": old_class,
        "new_class": new_class,
        "kept_inputs": sorted(name for name in kept if name not in applied),
        "dropped_inputs": dropped,
        "applied_inputs": applied,
        "missing_inputs": sorted(name for name in accepted if name not in kept),
    }
    return {"workflow": updated, "changes": changes}


@function_tool
async def replace_node_class(node_id: str, new_class_type: str, inputs_json: str = "{}") -> str:
    """Change a node's class_type in the session workflow, keeping every link and value the new class accepts.
    inputs_json: JSON object of inputs to set on the new class (e.g. {"tile_size": 512}). Inputs the new class
    does not declare are dropped and listed in the result; required inputs it lacks are listed as missing_inputs."""
    try:
        try:
            inputs = json.loads(inputs_json) if inputs_json and inputs_json.strip() else {}
        except json.JSONDecodeError as e:
            return tool_json({"error": f"inputs_json is not valid JSON: {e}"})
        if not isinstance(inputs, dict):
            return tool_json({"error": "inputs_json must be a JSON object"})

        found = session_workflow()
        if isinstance(found, dict):
            return tool_json(found)
        session_id, workflow = found

        object_info = await ComfyGateway().get_object_info(new_class_type)
        result = replace_node_class_in(workflow, object_info, node_id, new_class_type, inputs)
        if "error" in result:
            return tool_json(result)

        from ..dao.workflow_table import save_workflow_data
        from ..service.workflow_rewrite_tools import get_workflow_identity_from_config, repin_workflow_version
        config = get_config() or {"session_id": session_id}
        workflow_key, workflow_hash = get_workflow_identity_from_config(config)
        write_attributes = {
            "action": "replace_node_class",
            "description": f"Replaced {result['changes']['old_class']} with {new_class_type} in node {node_id}",
            "changes": result["changes"],
        }
        if workflow_key:
            write_attributes["workflow_key"] = workflow_key
        if workflow_hash:
            write_attributes["workflow_hash"] = workflow_hash
        version_id = save_workflow_data(
            session_id,
            result["workflow"],
            workflow_data_ui=None,
            attributes=write_attributes,
        )
        # Later reads in this run (and the final debug checkpoint) should see this edit.
        repin_workflow_version(version_id)
        return tool_json({
            "success": True,
            "changes": result["changes"],
            "ext": [{"type": "workflow_update", "data": {
                "workflow_data": result["workflow"],
                "workflow_key": workflow_key,
                "workflow_hash": workflow_hash,
            }}],
        })
    except Exception as e:
        return tool_json({"error": f"Failed to replace node class: {e}"})
