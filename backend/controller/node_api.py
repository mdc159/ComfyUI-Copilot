"""Routes serving the offline node index: batch node info for the UI's Accept flow, and a manual refresh.

Mirrors the route-registration style of validate_api.py.
"""
import server
from aiohttp import web

from ..tools.node_index import NodeDatabaseUnavailable, get_index, node_rows_for_types


@server.PromptServer.instance.routes.post("/api/copilot/node_info_by_types")
async def node_info_by_types(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be {\"node_types\": [...]}"}, status=400)
    node_types = body.get("node_types") if isinstance(body, dict) else None
    if not isinstance(node_types, list):
        return web.json_response({"error": "body must be {\"node_types\": [...]}"}, status=400)
    try:
        data = await node_rows_for_types(node_types)
    except NodeDatabaseUnavailable as e:
        return web.json_response({"success": False, "message": str(e)})
    return web.json_response({"success": True, "data": data})


@server.PromptServer.instance.routes.post("/api/copilot/node_index/refresh")
async def refresh_index(request):
    try:
        index = await get_index(force=True)
    except NodeDatabaseUnavailable as e:
        return web.json_response({"success": False, "message": str(e)})
    return web.json_response({"success": True, "source": index.db_source, "records": len(index.records),
                              "installed": index.installed_count})
