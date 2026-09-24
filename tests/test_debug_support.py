"""Error classification, outcome rule, node class replacement and parameter value parsing (PR E).

Run with the standalone Python: python -m unittest discover -s tests -v
"""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.service.debug_agent import debug_outcome
from backend.service.parameter_tools import parse_parameter_value
from backend.tools.graph_edit import replace_node_class_in
from backend.tools.run_workflow import check_workflow
from backend.tools.runtime_errors import analyze_error, classify_runtime_error
from backend.utils.comfy_gateway import ComfyGateway, ComfyTarget, _target, set_target
from fake_comfy import OBJECT_INFO, FakeComfy

PROMPT = {
    '5': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 512, 'height': 512, 'batch_size': 1}},
    '9': {'class_type': 'KSampler', 'inputs': {'seed': 0, 'steps': 20, 'latent_image': ['5', 0]}},
    '10': {'class_type': 'SaveImage', 'inputs': {'images': ['9', 0]}},
}
WORKFLOW = {
    '3': {'class_type': 'KSampler', 'inputs': {'seed': 1, 'latent_image': ['5', 0]}},
    '4': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'ae.safetensors'}},
    '5': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 1024, 'height': 1024, 'batch_size': 1}},
    '8': {'class_type': 'VAEDecode', 'inputs': {'samples': ['3', 0], 'vae': ['4', 0]},
          '_meta': {'title': 'VAE Decode'}},
}


class ClassifyRuntimeErrorTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ('torch.OutOfMemoryError', 'CUDA out of memory. Tried to allocate 2.00 GiB', 'oom'),
            ('OutOfMemoryError', '', 'oom'),
            ('RuntimeError', 'Allocation on device', 'oom'),
            ('RuntimeError', 'cutlass_fp16_linear: K mismatch (expected 2048, got 768)', 'dtype'),
            ('RuntimeError', 'expected scalar type Half but found BFloat16', 'dtype'),
            ('RuntimeError', 'mat1 and mat2 shapes cannot be multiplied (77x768 and 2048x1280)', 'dtype'),
            ('RuntimeError', 'Sizes of tensors must match except in dimension 1', 'shape'),
            ('RuntimeError', 'Error(s) in loading state_dict: size mismatch for weight', 'shape'),
            ('FileNotFoundError', "[Errno 2] No such file or directory: 'x.png'", 'missing_file'),
            ('RuntimeError', 'No such file or directory', 'missing_file'),
            ('KeyError', "'clip'", 'other'),
            ('', '', 'other'),
        ]
        for exception_type, message, expected in cases:
            with self.subTest(exception_type=exception_type, message=message):
                self.assertEqual(classify_runtime_error(exception_type, message), expected)

    def test_none_inputs(self):
        self.assertEqual(classify_runtime_error(None, None), 'other')


class AnalyzeErrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeComfy(execution='error')
        await self.fake.start()
        self.addAsyncCleanup(self.fake.stop)
        self.token = set_target(ComfyTarget(self.fake.base_url, name='fake'))
        self.addCleanup(_target.reset, self.token)

    async def test_execute_result_recommends_runtime_error_agent(self):
        result = await check_workflow(ComfyGateway(), copy.deepcopy(PROMPT), 'execute', timeout=5, poll=0.01)
        self.assertEqual(result['status'], 'execution_error')
        analysis = analyze_error(result)
        self.assertEqual(analysis['recommended_agent'], 'runtime_error_agent')
        self.assertEqual(analysis['error_type'], 'runtime_dtype')
        self.assertEqual(analysis['affected_nodes'], ['9'])
        self.assertEqual(analysis['error_details'][0]['node_type'], 'KSampler')
        # The tool receives the result as a JSON string; same answer.
        import json
        self.assertEqual(analyze_error(json.dumps(result))['recommended_agent'], 'runtime_error_agent')

    def test_oom_execute_result(self):
        result = {'mode': 'execute', 'status': 'execution_error', 'success': False, 'execution_error': {
            'node_id': '8', 'node_type': 'VAEDecode', 'exception_type': 'torch.OutOfMemoryError',
            'exception_message': 'CUDA out of memory.', 'traceback_tail': [], 'executed': ['3']}}
        analysis = analyze_error(result)
        self.assertEqual((analysis['error_type'], analysis['affected_nodes']), ('runtime_oom', ['8']))

    def test_validation_result_keeps_legacy_routing(self):
        valid = analyze_error('{"mode": "validate", "status": "valid", "success": true}')
        self.assertEqual((valid['error_type'], valid['recommended_agent']), ('no_error', 'none'))
        failed = analyze_error('{"success": false, "node_errors": {"9": {"errors": [{"type": "required_input_missing", '
                               '"message": "Required input is missing"}]}}}')
        self.assertEqual(failed['recommended_agent'], 'link_agent')
        self.assertEqual(failed['affected_nodes'], ['9'])
        param = analyze_error('{"success": false, "node_errors": {"4": {"errors": [{"type": "value_not_in_list"}]}}}')
        self.assertEqual(param['recommended_agent'], 'parameter_agent')


class DebugOutcomeTests(unittest.TestCase):
    def test_executed(self):
        runs = [{'mode': 'validate', 'status': 'valid'}, {'mode': 'execute', 'status': 'execution_error'},
                {'mode': 'validate', 'status': 'valid'}, {'mode': 'execute', 'status': 'success'}]
        self.assertEqual(debug_outcome(runs, None), 'executed')
        self.assertEqual(debug_outcome(runs, {'reason': 'stale'}), 'executed')

    def test_limitation(self):
        runs = [{'mode': 'validate', 'status': 'valid'}, {'mode': 'execute', 'status': 'execution_error'}]
        self.assertEqual(debug_outcome(runs, {'reason': 'needs --lowvram', 'next_steps': 'edit the bat'}), 'limitation')

    def test_unresolved(self):
        self.assertEqual(debug_outcome([], None), 'unresolved')
        self.assertEqual(debug_outcome([{'mode': 'execute', 'status': 'execution_error'}], None), 'unresolved')
        # A success followed by a failing execute run is not executed.
        self.assertEqual(debug_outcome([{'mode': 'execute', 'status': 'success'},
                                        {'mode': 'execute', 'status': 'timeout'}], None), 'unresolved')

    def test_validate_only_success_is_unresolved(self):
        self.assertEqual(debug_outcome([{'mode': 'validate', 'status': 'valid', 'success': True}], None), 'unresolved')


class ReplaceNodeClassTests(unittest.TestCase):
    def test_vae_decode_to_tiled_keeps_links_and_applies_inputs(self):
        original = copy.deepcopy(WORKFLOW)
        result = replace_node_class_in(WORKFLOW, OBJECT_INFO, '8', 'VAEDecodeTiled', {'tile_size': 512})
        self.assertNotIn('error', result)
        node = result['workflow']['8']
        self.assertEqual(node['class_type'], 'VAEDecodeTiled')
        self.assertEqual(node['inputs'], {'samples': ['3', 0], 'vae': ['4', 0], 'tile_size': 512})
        changes = result['changes']
        self.assertEqual(changes['old_class'], 'VAEDecode')
        self.assertEqual(changes['kept_inputs'], ['samples', 'vae'])
        self.assertEqual(changes['applied_inputs'], {'tile_size': 512})
        self.assertEqual(changes['dropped_inputs'], [])
        self.assertEqual(changes['missing_inputs'], ['overlap', 'temporal_overlap', 'temporal_size'])
        self.assertEqual(WORKFLOW, original)  # input not mutated
        self.assertEqual(result['workflow']['3'], WORKFLOW['3'])  # other nodes untouched

    def test_drops_input_missing_on_new_class(self):
        workflow = copy.deepcopy(WORKFLOW)
        workflow['8']['class_type'] = 'VAEDecodeTiled'
        workflow['8']['inputs'].update({'tile_size': 512, 'overlap': 64})
        result = replace_node_class_in(workflow, OBJECT_INFO, '8', 'VAEDecode', {})
        self.assertEqual(result['workflow']['8']['inputs'], {'samples': ['3', 0], 'vae': ['4', 0]})
        self.assertEqual(sorted(result['changes']['dropped_inputs']), ['overlap', 'tile_size'])

    def test_errors(self):
        self.assertIn('error', replace_node_class_in(WORKFLOW, OBJECT_INFO, '99', 'VAEDecodeTiled', {}))
        self.assertIn('error', replace_node_class_in(WORKFLOW, OBJECT_INFO, '8', 'NoSuchNode', {}))
        self.assertIn('error', replace_node_class_in(WORKFLOW, OBJECT_INFO, '8', 'VAEDecodeTiled', {'bogus': 1}))


class ParameterValueParsingTests(unittest.TestCase):
    def test_parsing(self):
        cases = [('false', False), ('true', True), ('512', 512), ('7.5', 7.5), ('null', None),
                 ('euler', 'euler'), ('sd_xl_base_1.0.safetensors', 'sd_xl_base_1.0.safetensors'),
                 ('"quoted"', 'quoted'), ('', ''), ('[1, 2]', [1, 2])]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(parse_parameter_value(raw), expected)
                self.assertIs(type(parse_parameter_value(raw)), type(expected))

    def test_non_string_passthrough(self):
        self.assertIs(parse_parameter_value(False), False)
        self.assertEqual(parse_parameter_value(3), 3)


if __name__ == '__main__':
    unittest.main()
