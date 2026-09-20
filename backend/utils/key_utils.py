from copy import deepcopy


def workflow_config_adapt(config: dict) -> dict:
    """Select the workflow connection without putting credentials in request context."""
    result = deepcopy(config or {})
    result['llm_role'] = 'workflow'
    result.pop('model_select', None)
    return result
