import asyncio
import ipaddress
from urllib.parse import urlparse

from aiohttp import web
import server

from .service import get_service


def authorize(request, mutation=False):
    peer = request.transport.get_extra_info('peername') if request.transport else None
    if not peer or not ipaddress.ip_address(peer[0]).is_loopback:
        raise web.HTTPForbidden(text='Model settings are available only on this computer')
    origin = request.headers.get('Origin')
    if origin and urlparse(origin).netloc != request.host:
        raise web.HTTPForbidden(text='Cross-origin model settings requests are not allowed')
    if mutation and (request.content_type != 'application/json' or request.headers.get('X-Copilot-Settings') != '1'):
        raise web.HTTPForbidden(text='Use the Copilot model settings interface')


@server.PromptServer.instance.routes.get('/api/copilot/llm')
async def settings(request):
    authorize(request)
    return web.json_response(await asyncio.to_thread(get_service().status))


@server.PromptServer.instance.routes.post('/api/copilot/llm')
async def update(request):
    authorize(request, True)
    data = await request.json()
    service = get_service()
    try:
        action = data.get('action')
        if action == 'connection':
            return web.json_response({'id': await asyncio.to_thread(service.save_connection, data)})
        if action == 'defaults':
            await asyncio.to_thread(service.save_defaults, data.get('chat'), data.get('workflow'))
            return web.json_response({'saved': True})
        if action == 'login':
            return web.json_response(await service.start_login(data['connection']))
        if action == 'reply':
            await service.reply_login(data['id'], data['prompt'], data['value'])
            return web.json_response({'accepted': True})
        if action == 'cancel':
            state = service.signins[data['id']]
            state['task'].cancel()
            return web.json_response({'cancelled': True})
        if action == 'disconnect':
            connection = service.connection(data['connection'])
            for state in service.signins.values():
                if state['connection'] == connection['id'] and state['status'] == 'pending':
                    state['task'].cancel()
            def remove():
                with service.connection_lock(connection['id']):
                    service.credentials.delete(connection['id'])
            await asyncio.to_thread(remove)
            return web.json_response({'removed': True})
        if action == 'test':
            result = await service.call(data['connection'], {'action': 'complete', 'model': data['model'],
                'messages': [{'role': 'user', 'content': 'Reply with exactly OK. Do not call tools.'}], 'maxTokens': 64})
            return web.json_response({'verified': True, 'reply': result['text']})
        raise ValueError('Unknown model settings action')
    except (ValueError, KeyError) as error:
        return web.json_response({'error': str(error)}, status=400)


@server.PromptServer.instance.routes.get('/api/copilot/llm/models')
async def catalog(request):
    authorize(request)
    try:
        models = await get_service().catalog(request.query['connection'])
        return web.json_response({'models': models})
    except Exception:
        return web.json_response({'error': 'Could not load models. Check the connection, endpoint, and credentials.'}, status=400)


@server.PromptServer.instance.routes.get('/api/copilot/llm/login/{id}')
async def login_status(request):
    authorize(request)
    state = get_service().signins.get(request.match_info['id'])
    if not state:
        raise web.HTTPNotFound()
    return web.json_response(get_service().public_signin(state))
