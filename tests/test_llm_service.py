"""Regression checks for credentials and the existing Agents SDK tool loop.

Run with the standalone Python: python -m unittest discover -s tests -v
The HTTP model and OAuth runtime in these tests are deterministic local fixtures.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from aiohttp import web
from agents import Agent, Runner, function_tool, set_tracing_disabled
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.llm.model import CopilotModel
from backend.llm.service import ModelService

set_tracing_disabled(True)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = ModelService(self.directory.name)

    def test_dpapi_roundtrip_and_no_public_secrets(self):
        s = self.service
        s.save_connection({'id': 'openai', 'api_key': 'fixture-private-key-not-real'})
        reloaded = ModelService(self.directory.name)
        self.assertEqual(reloaded.credentials.read('openai')['key'], 'fixture-private-key-not-real')
        for path in Path(self.directory.name).rglob('*'):
            if path.is_file():
                self.assertNotIn(b'fixture-private-key-not-real', path.read_bytes())
        self.assertNotIn('fixture-private-key-not-real', json.dumps(reloaded.status()))
        s.credentials.delete('openai')
        self.assertIsNone(s.credentials.read('openai'))

    def test_reject_path_and_credential_in_endpoint(self):
        for values in ({'id': '../escape'}, {'base_url': 'http://key:secret@localhost/v1'},
                       {'base_url': 'http://localhost/v1?api_key=secret'},
                       {'base_url': 'file:///tmp/key'}, {'id': 'openai', 'env': 'BAD NAME'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.service.save_connection(values)

    def test_subscription_never_uses_ambient_api_key(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fixture-ambient-key'}):
            with self.assertRaisesRegex(ValueError, 'fallback is disabled'):
                self.service._request('openai-codex-login', {'action': 'complete'})
            _, request = self.service._request('openai-codex-login', {'action': 'catalog'})
            self.assertIsNone(request['credential'])

    def test_workflow_selection_and_persistence(self):
        s = self.service
        s.save_defaults({'connection': 'ollama', 'model': 'local'},
                        {'connection': 'lmstudio', 'model': 'workflow'})
        self.assertEqual(s.selection({'model_select': 'other'})['model'], 'other')
        self.assertEqual(s.selection({'llm_role': 'workflow'})['model'], 'workflow')
        self.assertEqual(ModelService(self.directory.name).selection()['connection'], 'ollama')


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = ModelService(self.directory.name)
        self.requests = []
        self.headers = []
        app = web.Application()
        app.router.add_get('/v1/models', self.models)
        app.router.add_post('/v1/chat/completions', self.complete)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.service.save_connection({'id': 'lmstudio', 'base_url': f'http://127.0.0.1:{port}/v1',
                                      'api_key': 'fixture-local-server-key'})
        self.service.save_defaults({'connection': 'lmstudio', 'model': 'fixture-model'}, None)

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def models(self, request):
        self.headers.append(request.headers.get('Authorization'))
        return web.json_response({'data': [{'id': 'fixture-model'}]})

    async def complete(self, request):
        self.headers.append(request.headers.get('Authorization'))
        payload = await request.json()
        self.requests.append(payload)
        messages = payload['messages']
        has_result = any(m['role'] == 'tool' for m in messages)
        use_tool = bool(payload.get('tools')) and not has_result
        if use_tool:
            delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'call_fixture', 'type': 'function',
                      'function': {'name': 'add', 'arguments': '{"a":2,"b":3}'}}]}
        else:
            text = '5' if has_result else ('{"summary":"fixture summary"}'
                    if any('schema' in str(m.get('content', '')).lower() for m in messages) else 'LOCAL_OK')
            delta = {'role': 'assistant', 'content': text}
        stream = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await stream.prepare(request)
        for body in (
            {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture-model',
             'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
            {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture-model',
             'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if use_tool else 'stop'}],
             'usage': {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}},
        ):
            await stream.write(('data: ' + json.dumps(body) + '\n\n').encode())
        await stream.write(b'data: [DONE]\n\n')
        return stream

    async def test_local_catalog_stream_and_auth(self):
        self.assertEqual((await self.service.catalog('lmstudio'))[0]['id'], 'fixture-model')
        agent = Agent(name='fixture', model=CopilotModel(service=self.service))
        result = Runner.run_streamed(agent, 'Hello')
        deltas = []
        async for event in result.stream_events():
            if event.type == 'raw_response_event' and event.data.type == 'response.output_text.delta':
                deltas.append(event.data.delta)
        self.assertEqual(result.final_output, 'LOCAL_OK')
        self.assertEqual(''.join(deltas), 'LOCAL_OK')
        self.assertTrue(all(h == 'Bearer fixture-local-server-key' for h in self.headers))
        self.assertEqual(result.context_wrapper.usage.total_tokens, 13)

    async def test_existing_tool_runner_executes_then_resumes(self):
        calls = []
        @function_tool
        def add(a: int, b: int) -> int:
            """Add two fixture values."""
            calls.append((a, b))
            return a + b
        agent = Agent(name='fixture', model=CopilotModel(service=self.service), tools=[add])
        result = Runner.run_streamed(agent, 'Add 2 and 3')
        async for _ in result.stream_events():
            pass
        self.assertEqual(result.final_output, '5')
        self.assertEqual(calls, [(2, 3)])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(next(m for m in self.requests[1]['messages'] if m['role'] == 'tool')['content'], '5')

    async def test_structured_output(self):
        class Summary(BaseModel):
            summary: str
        agent = Agent(name='fixture', model=CopilotModel(service=self.service), output_type=Summary)
        result = await Runner.run(agent, 'Summarize')
        self.assertEqual(result.final_output.summary, 'fixture summary')

    async def test_native_oauth_catalog_without_signin(self):
        rows = await self.service.catalog('openai-codex-login')
        self.assertTrue(rows)

    async def test_refresh_saved_before_provider_error(self):
        fixture = Path(self.directory.name) / 'refresh.mjs'
        fixture.write_text('''process.stdin.once('data', () => {
          console.log(JSON.stringify({type:'credential',value:{type:'oauth',access:'new-access',refresh:'new-refresh',expires:123}}));
          console.log(JSON.stringify({type:'error',message:'fixture provider failure'}));
        });''')
        self.service.credentials.write('openai-codex-login', {'type': 'oauth', 'access': 'old'})
        with patch('backend.llm.service.RUNTIME', fixture):
            with self.assertRaisesRegex(ValueError, 'fixture provider failure'):
                await self.service.call('openai-codex-login', {'action': 'complete'})
        self.assertEqual(self.service.credentials.read('openai-codex-login')['access'], 'new-access')
        self.assertFalse(self.service.connection_lock('openai-codex-login').locked())

    async def test_login_prompt_reply_and_encrypted_persistence(self):
        fixture = Path(self.directory.name) / 'login.mjs'
        fixture.write_text('''import {createInterface} from 'node:readline';
        let started=false;
        createInterface({input:process.stdin}).on('line', line => {
          const value=JSON.parse(line);
          if (!started) { started=true; console.log(JSON.stringify({type:'prompt',id:'step',event:{type:'text',message:'Fixture code'}})); }
          else { console.log(JSON.stringify({type:'credential',value:{type:'oauth',access:value.value,refresh:'fixture'}}));
            console.log(JSON.stringify({type:'result',value:{connected:true}})); process.exit(0); }
        });''')
        with patch('backend.llm.service.RUNTIME', fixture):
            public = await self.service.start_login('openai-codex-login')
            state = self.service.signins[public['id']]
            async with asyncio.timeout(10):
                while state['prompt'] is None:
                    await asyncio.sleep(.05)
                await self.service.reply_login(state['id'], 'step', 'fixture-private-code')
                await state['task']
        self.assertEqual(state['status'], 'connected')
        self.assertNotIn('fixture-private-code', json.dumps(self.service.public_signin(state)))
        self.assertEqual(self.service.credentials.read('openai-codex-login')['access'], 'fixture-private-code')

    async def test_cancellation_releases_connection(self):
        fixture = Path(self.directory.name) / 'pending.mjs'
        fixture.write_text("setInterval(() => {}, 1000)")
        with patch('backend.llm.service.RUNTIME', fixture):
            public = await self.service.start_login('openai-codex-login')
            state = self.service.signins[public['id']]
            async with asyncio.timeout(10):
                while 'process' not in state:
                    await asyncio.sleep(.05)
                state['task'].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await state['task']
        self.assertEqual(state['status'], 'cancelled')
        self.assertIsNotNone(state['process'].returncode)
        self.assertFalse(self.service.connection_lock('openai-codex-login').locked())

    async def test_runtime_crash_logs_redacted_stderr(self):
        self.service.save_connection({'id': 'openai', 'api_key': 'fixture-private-key-not-real'})
        fixture = Path(self.directory.name) / 'crash.mjs'
        fixture.write_text("process.stdin.once('data', d => { const r = JSON.parse(d);"
                           " console.error('boom with ' + r.credential.key); process.exit(3); });")
        with patch('backend.llm.service.RUNTIME', fixture), self.assertLogs('comfyui_copilot', 'WARNING') as logs:
            with self.assertRaisesRegex(ValueError, 'exited before completing'):
                await self.service.call('openai', {'action': 'complete', 'model': 'm', 'messages': []})
        output = '\n'.join(logs.output)
        self.assertIn('code 3', output)
        self.assertIn('boom with [redacted]', output)
        self.assertNotIn('fixture-private-key-not-real', output)
        self.assertFalse(self.service.connection_lock('openai').locked())

    async def test_concurrent_completions_on_one_connection(self):
        self.service.save_connection({'id': 'openai', 'api_key': 'fixture-private-key-not-real'})
        fixture = Path(self.directory.name) / 'slow.mjs'
        fixture.write_text('''process.stdin.once('data', () => {
          console.log(JSON.stringify({type:'delta',value:'x'}));
          setTimeout(() => { console.log(JSON.stringify({type:'result',value:'done'})); process.exit(0); }, 600);
        });''')
        payload = {'action': 'complete', 'model': 'm', 'messages': []}
        with patch('backend.llm.service.RUNTIME', fixture):
            started = asyncio.get_running_loop().time()
            results = await asyncio.gather(self.service.call('openai', payload), self.service.call('openai', payload))
            elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(results, ['done', 'done'])
        # Serialized streams would take at least 1.2 s; the lock is released once output begins.
        self.assertLess(elapsed, 1.2)
        self.assertFalse(self.service.connection_lock('openai').locked())

    async def test_auth_phase_is_serialized(self):
        self.service.save_connection({'id': 'openai', 'api_key': 'fixture-private-key-not-real'})
        fixture = Path(self.directory.name) / 'refresh-slow.mjs'
        fixture.write_text('''process.stdin.once('data', d => {
          const r = JSON.parse(d);
          setTimeout(() => {
            console.log(JSON.stringify({type:'credential',value:{type:'api_key',key:'refreshed-' + r.model}}));
            console.log(JSON.stringify({type:'result',value:r.model}));
            process.exit(0);
          }, 300);
        });''')
        written = []
        original = self.service.credentials.write
        def record(connection_id, value):
            written.append(value['key'])
            original(connection_id, value)
        with patch('backend.llm.service.RUNTIME', fixture), patch.object(self.service.credentials, 'write', record):
            started = asyncio.get_running_loop().time()
            results = await asyncio.gather(
                self.service.call('openai', {'action': 'complete', 'model': 'first', 'messages': []}),
                self.service.call('openai', {'action': 'complete', 'model': 'second', 'messages': []}))
            elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(results, ['first', 'second'])
        self.assertGreaterEqual(elapsed, 0.6)
        self.assertEqual(written, ['refreshed-first', 'refreshed-second'])
        self.assertEqual(self.service.credentials.read('openai')['key'], 'refreshed-second')
        self.assertFalse(self.service.connection_lock('openai').locked())


class RouteTests(unittest.TestCase):
    def test_local_csrf_boundary(self):
        fake = types.SimpleNamespace(PromptServer=types.SimpleNamespace(instance=types.SimpleNamespace(routes=web.RouteTableDef())))
        with patch.dict(sys.modules, {'server': fake}):
            from backend.llm.routes import authorize
        def request(ip='127.0.0.1', origin='http://127.0.0.1:8188', marker='1'):
            return types.SimpleNamespace(transport=types.SimpleNamespace(get_extra_info=lambda _: (ip, 1234)),
                headers={'Origin': origin, 'X-Copilot-Settings': marker}, host='127.0.0.1:8188', content_type='application/json')
        authorize(request(), True)
        for args in ({'ip': '192.168.1.5'}, {'origin': 'https://unrelated.example'}, {'marker': ''}):
            with self.subTest(args=args), self.assertRaises(web.HTTPForbidden):
                authorize(request(**args), True)


if __name__ == '__main__':
    unittest.main()
