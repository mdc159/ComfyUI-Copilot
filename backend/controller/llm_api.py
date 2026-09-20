from aiohttp import web
import server
from ..llm.service import get_service
from ..llm import routes  # Register the owned settings endpoints.


@server.PromptServer.instance.routes.get('/api/model_config')
async def list_models(request):
    service = get_service()
    selected = service.selection()
    try:
        rows = await service.catalog(selected['connection'])
        rows.sort(key=lambda row: row['id'] != selected['model'])
        return web.json_response({'models': [{'label': row['name'], 'name': row['id'],
            'image_enable': row.get('image', False)} for row in rows], 'default_model': selected['model']})
    except Exception:
        return web.json_response({'models': [], 'error': 'Configure a working model connection in Copilot settings'}, status=400)
