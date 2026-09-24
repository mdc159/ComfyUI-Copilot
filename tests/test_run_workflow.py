"""check_workflow / summarize_history / system_stats against the in-process ComfyUI double.

Run with the standalone Python: python -m unittest discover -s tests -v
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.tools.run_workflow import TRACEBACK_TAIL, UNSUPPORTED_REASON, check_workflow, summarize_history
from backend.tools.system import system_stats
from backend.utils.comfy_gateway import ComfyGateway, ComfyTarget, _target, set_target
from fake_comfy import CUTLASS_ERROR, FakeComfy

PROMPT = {
    '5': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 512, 'height': 512, 'batch_size': 1}},
    '9': {'class_type': 'KSampler', 'inputs': {'seed': 0, 'steps': 20, 'latent_image': ['5', 0]}},
    '10': {'class_type': 'SaveImage', 'inputs': {'images': ['9', 0]}},
}
NODE_ERRORS = {'9': {'errors': [{'type': 'required_input_missing', 'message': 'Required input is missing',
                                 'details': 'model', 'extra_info': {'input_name': 'model'}}],
                     'dependent_outputs': ['10'], 'class_type': 'KSampler'}}
ALWAYS = ('success', 'prompt_id', 'elapsed_seconds', 'target', 'mode', 'status')


class SummarizeHistoryTests(unittest.TestCase):
    def test_success_caps_outputs_and_drops_meta(self):
        images = [{'filename': f'ComfyUI_{i:05d}_.png', 'subfolder': '', 'type': 'output'} for i in range(6)]
        entry = {'status': {'status_str': 'success', 'completed': True, 'messages': [['execution_success', {}]]},
                 'outputs': {'10': {'images': images, 'text': 'kept'}}, 'meta': {'10': {'node_id': '10'}}}
        summary = summarize_history(entry)
        self.assertEqual(summary['status'], 'success')
        self.assertEqual(summary['outputs']['10']['images'], images[:4])
        self.assertEqual(summary['outputs']['10']['text'], 'kept')
        self.assertNotIn('meta', summary)

    def test_execution_error_is_trimmed(self):
        entry = {'status': {'status_str': 'error', 'completed': False, 'messages': [
            ['execution_start', {'prompt_id': 'p'}],
            ['execution_error', {'prompt_id': 'p', 'node_id': '9', 'node_type': 'KSampler', 'executed': ['5'],
                                 'current_inputs': {'seed': [0]}, 'current_outputs': ['5'], **CUTLASS_ERROR}]]},
            'outputs': {}}
        summary = summarize_history(entry)
        self.assertEqual(summary['status'], 'execution_error')
        error = summary['execution_error']
        self.assertEqual(sorted(error), ['exception_message', 'exception_type', 'executed', 'node_id', 'node_type', 'traceback_tail'])
        self.assertEqual(len(error['traceback_tail']), TRACEBACK_TAIL)
        self.assertEqual(error['traceback_tail'], CUTLASS_ERROR['traceback'][-TRACEBACK_TAIL:])

    def test_status_without_messages(self):
        summary = summarize_history({'status': {'status_str': 'error', 'messages': []}})
        self.assertEqual(summary['status'], 'execution_error')
        self.assertIsNone(summary['execution_error'])


class CheckWorkflowTests(unittest.IsolatedAsyncioTestCase):
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

    async def check(self, mode, fake=None, **kwargs):
        gateway = ComfyGateway(ComfyTarget(fake.base_url, name='fake')) if fake else self.gateway
        result = await check_workflow(gateway, PROMPT, mode, poll=0.02, **kwargs)
        for key in ALWAYS:
            self.assertIn(key, result, result)
        self.assertEqual((result['mode'], result['target']), (mode, 'fake'))
        self.assertEqual(result['success'], result['status'] in ('valid', 'success'))
        self.assertGreaterEqual(result['elapsed_seconds'], 0)
        return result

    async def test_execute_validation_failed(self):
        fake = await self.start_fake(node_errors=NODE_ERRORS)
        result = await self.check('execute', fake)
        self.assertEqual(result['status'], 'validation_failed')
        self.assertFalse(result['success'])
        self.assertEqual(result['node_errors'], NODE_ERRORS)
        self.assertEqual(result['error']['type'], 'prompt_outputs_failed_validation')
        self.assertIsNone(result['prompt_id'])
        self.assertEqual(fake.history, {})

    async def test_execute_runtime_error(self):
        fake = await self.start_fake(execution='error')
        result = await self.check('execute', fake, timeout=5)
        self.assertEqual(result['status'], 'execution_error')
        self.assertFalse(result['success'])
        self.assertIn(result['prompt_id'], fake.history)
        error = result['execution_error']
        self.assertEqual((error['node_id'], error['node_type'], error['exception_type']), ('9', 'KSampler', 'RuntimeError'))
        self.assertEqual(error['exception_message'], CUTLASS_ERROR['exception_message'])
        self.assertEqual(error['executed'], ['5', '10'])
        self.assertEqual(len(error['traceback_tail']), 15)
        self.assertEqual(error['traceback_tail'][-1], CUTLASS_ERROR['traceback'][-1])
        self.assertNotIn('current_inputs', error)
        self.assertNotIn('current_outputs', error)

    async def test_execute_success(self):
        result = await self.check('execute', timeout=5, client_id='ui-client')
        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['success'])
        self.assertEqual(list(result['outputs']), ['10'])
        self.assertLessEqual(len(result['outputs']['10']['images']), 4)
        self.assertNotIn('meta', result)
        self.assertEqual(self.fake.prompts[0]['client_id'], 'ui-client')
        self.assertIn(result['prompt_id'], self.fake.history)

    async def test_execute_default_client_id(self):
        await self.check('execute', timeout=5)
        self.assertTrue(self.fake.prompts[0]['client_id'].startswith('copilot_'))

    async def test_execute_interrupted(self):
        fake = await self.start_fake(execution='interrupted')
        result = await self.check('execute', fake, timeout=5)
        self.assertEqual(result['status'], 'interrupted')
        self.assertFalse(result['success'])
        self.assertEqual((result['node_id'], result['node_type']), ('9', 'KSampler'))

    async def test_execute_timeout_cancels(self):
        fake = await self.start_fake(execution='hang')
        result = await self.check('execute', fake, timeout=0.2)
        self.assertEqual(result['status'], 'timeout')
        self.assertFalse(result['success'])
        prompt_id = result['prompt_id']
        self.assertEqual(fake.interrupts, [prompt_id])
        self.assertEqual(fake.deleted, [prompt_id])
        entry = await ComfyGateway(ComfyTarget(fake.base_url)).wait_for_prompt(prompt_id, timeout=5, poll=0.02)
        self.assertEqual(entry['status']['messages'][-1][0], 'execution_interrupted')

    async def test_execute_unreachable(self):
        dead = FakeComfy()
        await dead.start()
        await dead.stop()
        result = await self.check('execute', dead)
        self.assertEqual(result['status'], 'unreachable')
        self.assertFalse(result['success'])
        self.assertIsNone(result['prompt_id'])

    async def test_validate_valid(self):
        result = await self.check('validate')
        self.assertEqual(result['status'], 'valid')
        self.assertTrue(result['success'])
        self.assertEqual((result['error'], result['node_errors'], result['outputs']), (None, {}, ['10']))
        self.assertEqual(self.fake.prompts, [])   # nothing queued

    async def test_validate_failed(self):
        fake = await self.start_fake(node_errors=NODE_ERRORS)
        result = await self.check('validate', fake)
        self.assertEqual(result['status'], 'validation_failed')
        self.assertFalse(result['success'])
        self.assertEqual(result['node_errors'], NODE_ERRORS)
        self.assertEqual(fake.prompts, [])

    async def test_validate_without_route(self):
        fake = await self.start_fake(validate_route=False)
        result = await self.check('validate', fake)
        self.assertEqual(result['status'], 'unsupported')
        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], UNSUPPORTED_REASON)
        self.assertEqual(fake.prompts, [])

    async def test_validate_unreachable(self):
        dead = FakeComfy()
        await dead.start()
        await dead.stop()
        result = await self.check('validate', dead)
        self.assertEqual(result['status'], 'unreachable')

    async def test_bad_mode(self):
        with self.assertRaises(ValueError):
            await check_workflow(self.gateway, PROMPT, 'dry-run')

    async def test_system_stats_trimmed(self):
        stats = await system_stats(self.gateway)
        self.assertEqual(sorted(stats), ['argv', 'comfyui_version', 'devices', 'pytorch_version'])
        self.assertEqual(stats['comfyui_version'], '0.37.0')
        self.assertEqual(stats['argv'], ['ComfyUI\\main.py', '--windows-standalone-build'])
        self.assertEqual(sorted(stats['devices'][0]), ['name', 'vram_free_gb', 'vram_total_gb'])
        self.assertEqual(stats['devices'][0]['vram_total_gb'], 8.0)


if __name__ == '__main__':
    unittest.main()
