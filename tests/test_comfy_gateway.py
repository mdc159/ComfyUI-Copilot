"""Gateway target selection and the HTTP methods against the in-process ComfyUI double.

Run with the standalone Python: python -m unittest discover -s tests -v
"""
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.utils.comfy_gateway import (ComfyGateway, ComfyTarget, ComfyUnreachable, PromptLost, PromptTimeout,
                                         _target, get_target, set_target)
from fake_comfy import CUTLASS_ERROR, FakeComfy

PROMPT = {
    '5': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 512, 'height': 512, 'batch_size': 1}},
    '9': {'class_type': 'KSampler', 'inputs': {'seed': 0, 'steps': 20, 'latent_image': ['5', 0]}},
    '10': {'class_type': 'SaveImage', 'inputs': {'images': ['9', 0]}},
}
NODE_ERRORS = {'9': {'errors': [{'type': 'required_input_missing', 'message': 'Required input is missing',
                                 'details': 'model', 'extra_info': {'input_name': 'model'}}],
                     'dependent_outputs': ['10'], 'class_type': 'KSampler'}}


class TargetTests(unittest.TestCase):
    def test_precedence_contextvar_env_server(self):
        stub = types.SimpleNamespace(PromptServer=types.SimpleNamespace(instance=types.SimpleNamespace(address='0.0.0.0', port=9000)))
        with patch.dict(sys.modules, {'server': stub}), patch.dict(os.environ, {'COPILOT_COMFY_URL': 'http://env-host:1/'}):
            token = set_target(ComfyTarget('http://ctx-host:2', name='ctx'))
            try:
                self.assertEqual(get_target().base_url, 'http://ctx-host:2')
                self.assertEqual(ComfyGateway().target.name, 'ctx')
            finally:
                _target.reset(token)
            self.assertEqual(get_target().base_url, 'http://env-host:1')
            del os.environ['COPILOT_COMFY_URL']
            self.assertEqual(get_target().base_url, 'http://127.0.0.1:9000')
        with patch.dict(sys.modules, {'server': None}), patch.dict(os.environ, {'COPILOT_COMFY_URL': ''}):
            self.assertEqual(get_target(), ComfyTarget('http://127.0.0.1:8188'))

    def test_base_url_keeps_standalone_helpers_working(self):
        gateway = ComfyGateway(base_url='http://10.0.0.5:8188/')
        self.assertEqual((gateway.base_url, gateway.target.name), ('http://10.0.0.5:8188', 'custom'))


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = await self.start_fake()
        self.token = set_target(ComfyTarget(self.fake.base_url, name='fake'))
        self.gateway = ComfyGateway()

    async def asyncTearDown(self):
        _target.reset(self.token)

    async def start_fake(self, **kwargs):
        fake = FakeComfy(**kwargs)
        await fake.start()
        self.addAsyncCleanup(fake.stop)
        return fake

    async def queue(self, fake=None):
        gateway = ComfyGateway(ComfyTarget(fake.base_url, name='fake')) if fake else self.gateway
        result = await gateway.run_prompt({'prompt': PROMPT, 'client_id': 'fixture'})
        self.assertTrue(result['success'], result)
        return gateway, result['prompt_id']

    async def test_run_prompt_queued(self):
        _, prompt_id = await self.queue()
        self.assertEqual(self.fake.prompts[0]['client_id'], 'fixture')
        self.assertIn(prompt_id, self.fake.pending | self.fake.running | set(self.fake.history))

    async def test_run_prompt_validation_failure(self):
        fake = await self.start_fake(node_errors=NODE_ERRORS)
        result = await ComfyGateway(ComfyTarget(fake.base_url)).run_prompt({'prompt': PROMPT})
        self.assertFalse(result['success'])
        self.assertEqual(result['node_errors'], NODE_ERRORS)
        self.assertEqual(result['error']['type'], 'prompt_outputs_failed_validation')
        self.assertEqual(fake.history, {})

    async def test_unreachable_target(self):
        dead = FakeComfy()
        await dead.start()
        await dead.stop()
        gateway = ComfyGateway(ComfyTarget(dead.base_url, name='dead'))
        result = await gateway.run_prompt({'prompt': PROMPT})
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['type'], 'connection_error')
        self.assertEqual(await gateway.get_object_info(), {})
        with self.assertRaises(ComfyUnreachable):
            await gateway.get_system_stats()

    async def test_wait_for_prompt_success(self):
        gateway, prompt_id = await self.queue()
        entry = await gateway.wait_for_prompt(prompt_id, timeout=5, poll=0.02)
        self.assertEqual(entry['status']['status_str'], 'success')
        self.assertTrue(entry['status']['completed'])
        self.assertEqual([event for event, _ in entry['status']['messages']], ['execution_start', 'execution_success'])
        self.assertEqual(list(entry['outputs']), ['10'])
        self.assertEqual(entry['prompt'][1], prompt_id)

    async def test_wait_for_prompt_execution_error(self):
        fake = await self.start_fake(execution='error')
        gateway, prompt_id = await self.queue(fake)
        entry = await gateway.wait_for_prompt(prompt_id, timeout=5, poll=0.02)
        self.assertEqual(entry['status']['status_str'], 'error')
        event, data = entry['status']['messages'][-1]
        self.assertEqual(event, 'execution_error')
        self.assertEqual((data['node_id'], data['node_type']), ('9', 'KSampler'))
        self.assertEqual(data['exception_message'], CUTLASS_ERROR['exception_message'])
        self.assertEqual(len(data['traceback']), 20)
        self.assertEqual(entry['outputs'], {})

    async def test_wait_for_prompt_lost(self):
        fake = await self.start_fake(execution='hang')
        gateway, _ = await self.queue(fake)           # occupies the single runner
        _, waiting = await self.queue(fake)           # stays pending
        self.assertEqual(await gateway.manage_queue(delete=[waiting]), {'success': True})
        self.assertEqual(fake.deleted, [waiting])
        with self.assertRaises(PromptLost):
            await gateway.wait_for_prompt(waiting, timeout=5, poll=0.02)

    async def test_wait_for_prompt_timeout_then_cancel(self):
        fake = await self.start_fake(execution='hang')
        gateway, prompt_id = await self.queue(fake)
        with self.assertRaises(PromptTimeout):
            await gateway.wait_for_prompt(prompt_id, timeout=0.15, poll=0.03)
        await gateway.cancel_prompt(prompt_id)
        entry = await gateway.wait_for_prompt(prompt_id, timeout=5, poll=0.02)
        self.assertEqual(entry['status']['messages'][-1][0], 'execution_interrupted')
        self.assertFalse(entry['status']['completed'])

    async def test_cancel_prompt_hits_delete_and_interrupt(self):
        await self.gateway.cancel_prompt('abc')
        self.assertEqual((self.fake.deleted, self.fake.interrupts), (['abc'], ['abc']))

    async def test_validate_prompt(self):
        result = await self.gateway.validate_prompt(PROMPT)
        self.assertEqual(result, {'supported': True, 'valid': True, 'error': None, 'node_errors': {}, 'outputs': ['10']})
        fake = await self.start_fake(node_errors=NODE_ERRORS)
        result = await ComfyGateway(ComfyTarget(fake.base_url)).validate_prompt(PROMPT)
        self.assertEqual((result['supported'], result['valid'], result['node_errors']), (True, False, NODE_ERRORS))
        self.assertEqual(fake.prompts, [])   # nothing queued

    async def test_validate_prompt_without_route(self):
        fake = await self.start_fake(validate_route=False)
        self.assertEqual(await ComfyGateway(ComfyTarget(fake.base_url)).validate_prompt(PROMPT), {'supported': False})

    async def test_existing_methods_keep_shapes(self):
        gateway = self.gateway
        self.assertEqual(sorted(await gateway.get_installed_nodes()), ['EmptyLatentImage', 'KSampler', 'VAEDecode', 'VAEDecodeTiled'])
        self.assertIn('tile_size', (await gateway.get_object_info('VAEDecodeTiled'))['VAEDecodeTiled']['input']['required'])
        self.assertEqual(await gateway.get_object_info('Nope'), {})
        self.assertEqual(await gateway.get_queue_status(), {'queue_running': [], 'queue_pending': []})
        self.assertEqual(await gateway.get_history('missing'), {})
        self.assertEqual(await gateway.interrupt_processing(), {'success': True})
        self.assertEqual(self.fake.interrupts, [None])
        self.assertEqual((await gateway.get_system_stats())['devices'][0]['vram_total'], 8_585_216_000)


if __name__ == '__main__':
    unittest.main()
