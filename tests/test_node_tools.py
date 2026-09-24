"""PR H wiring: the plain functions behind the node-index @function_tool wrappers, and the pure
ext-extraction helper the mcp_client stream parser uses.

The @function_tool-wrapped functions (search_node, get_node_info, get_node_info_by_types) are not
directly callable as plain functions in tests -- they're wrapped as agents.tool FunctionTool
objects -- so these tests exercise the underlying plain functions (search_nodes, node_info,
node_rows_for_types) and assert the envelope shape the wrappers build on top of them.

Run with the standalone Python: python -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.tools.node_index import invalidate_index, node_info, node_rows_for_types, search_nodes
from backend.utils.comfy_gateway import ComfyTarget, _target, set_target
from fake_comfy import FakeComfy

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'node_db'
IMPACT = 'https://github.com/ltdrdata/ComfyUI-Impact-Pack'


class NodeToolEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    """Envelope-shape assertions for the plain functions the @function_tool wrappers delegate to,
    beyond what tests/test_node_index.py already covers."""

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='copilot-node-tools-'))
        self.db_dir = self.tmp / 'manager'
        shutil.copytree(FIXTURES, self.db_dir)
        self.env = patch.dict(os.environ, {'COPILOT_NODE_DB_DIR': str(self.db_dir),
                                           'COPILOT_NODE_INDEX_HOME': str(self.tmp / 'cache')})
        self.env.start()
        invalidate_index()
        self.fake = FakeComfy()
        await self.fake.start()
        self.token = set_target(ComfyTarget(self.fake.base_url, name='fake'))

    async def asyncTearDown(self):
        _target.reset(self.token)
        await self.fake.stop()
        invalidate_index()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_search_nodes_envelope_shape(self):
        result = await search_nodes('image resize')
        self.assertIn('answer', result)
        self.assertIn('data', result)
        self.assertEqual(result['ext'], [{'type': 'node', 'data': result['data']}])
        for row in result['data']:
            self.assertIn('name', row)

    async def test_search_nodes_empty_query_is_an_error_not_an_envelope(self):
        result = await search_nodes('')
        self.assertEqual(set(result), {'error'})

    async def test_node_info_envelope_has_node_type_ext(self):
        info = await node_info('ImageResizeKJ')
        self.assertEqual(info['ext'][0]['type'], 'node')
        self.assertEqual(info['ext'][0]['data'][0]['name'], 'ImageResizeKJ')

    async def test_get_node_info_by_types_envelope_shape(self):
        """The get_node_info_by_types wrapper assembles {"answer", "data", "missing", "ext"} on top
        of node_rows_for_types; exercise that assembly logic directly (mirrors the wrapper body)."""
        node_types = ['ImageResizeKJ', 'FaceDetailer', 'TotallyUnknownNode']
        rows = await node_rows_for_types(node_types)
        missing = [row['name'] for row in rows if not row.get('installed')]
        envelope = {
            'answer': f"{len(rows) - len(missing)}/{len(rows)} requested node types are installed",
            'data': rows,
            'missing': missing,
            'ext': [{'type': 'node', 'data': rows}],
        }
        self.assertEqual([r['name'] for r in envelope['data']], node_types)
        self.assertEqual(envelope['missing'], ['FaceDetailer', 'TotallyUnknownNode'])
        self.assertEqual(envelope['ext'], [{'type': 'node', 'data': rows}])
        self.assertIn('1/3', envelope['answer'])

    async def test_get_node_info_by_types_envelope_empty_input(self):
        rows = await node_rows_for_types([])
        missing = [row['name'] for row in rows if not row.get('installed')]
        answer = "no node types given" if not rows else "irrelevant"
        self.assertEqual(rows, [])
        self.assertEqual(missing, [])
        self.assertEqual(answer, "no node types given")


def _stub_comfy_modules():
    """backend.service.mcp_client pulls in the 'agents' package plus several backend modules; none of
    them import 'server'/'folder_paths' at module scope today, but stub them the same way
    tests/test_llm_service.py's RouteTests does so this test stays valid if that ever changes."""
    fake_server = types.SimpleNamespace(
        PromptServer=types.SimpleNamespace(instance=types.SimpleNamespace(routes=types.SimpleNamespace(
            post=lambda *_a, **_k: (lambda fn: fn)))))
    return {'server': fake_server, 'folder_paths': types.SimpleNamespace()}


class ExtractToolExtTests(unittest.TestCase):
    """The pure helper factored out of mcp_client's tool_call_output_item parser."""

    @classmethod
    def setUpClass(cls):
        with patch.dict(sys.modules, _stub_comfy_modules()):
            from backend.service.mcp_client import extract_tool_ext, _is_workflow_or_param_ext
        cls.extract_tool_ext = staticmethod(extract_tool_ext)
        cls._is_workflow_or_param_ext = staticmethod(_is_workflow_or_param_ext)

    def test_local_tool_envelope_returns_its_own_ext(self):
        # e.g. search_node's parsed JSON output
        payload = {"answer": "2 nodes matched 'resize'", "data": [{"name": "ImageResizeKJ"}],
                   "ext": [{"type": "node", "data": [{"name": "ImageResizeKJ"}]}]}
        extracted = self.extract_tool_ext(payload)
        self.assertEqual(extracted["ext"], payload["ext"])
        self.assertEqual(extracted["data"], payload["data"])
        self.assertEqual(extracted["answer"], payload["answer"])
        self.assertEqual(extracted["content_dict"], payload)

    def test_workflow_update_shaped_payload_is_not_swallowed_by_the_helper(self):
        # This shape must be handled by the OLD/existing branch in mcp_client (matched first via
        # _is_workflow_or_param_ext), not by extract_tool_ext -- confirm the helper steps aside.
        payload = {"ext": [{"type": "workflow_update", "data": {"nodes": []}}]}
        self.assertTrue(self._is_workflow_or_param_ext(payload["ext"]))
        self.assertIsNone(self.extract_tool_ext(payload))

        payload2 = {"ext": [{"type": "param_update", "data": {}}]}
        self.assertTrue(self._is_workflow_or_param_ext(payload2["ext"]))
        self.assertIsNone(self.extract_tool_ext(payload2))

    def test_legacy_text_envelope_from_remote_mcp_tool_still_parses(self):
        payload = {"text": '{"answer": "found it", "data": [{"id": 1}], "ext": [{"type": "workflow", "data": [{"id": 1}]}]}'}
        extracted = self.extract_tool_ext(payload)
        self.assertEqual(extracted["answer"], "found it")
        self.assertEqual(extracted["data"], [{"id": 1}])
        self.assertEqual(extracted["ext"], [{"type": "workflow", "data": [{"id": 1}]}])

    def test_legacy_text_envelope_with_list_payload(self):
        payload = {"text": '[{"id": 1}, {"id": 2}]'}
        extracted = self.extract_tool_ext(payload)
        self.assertIsNone(extracted["answer"])
        self.assertEqual(extracted["data"], [{"id": 1}, {"id": 2}])
        self.assertIsNone(extracted["ext"])

    def test_neither_shape_returns_none(self):
        self.assertIsNone(self.extract_tool_ext({"foo": "bar"}))
        self.assertIsNone(self.extract_tool_ext({"ext": []}))
        self.assertIsNone(self.extract_tool_ext({"text": ""}))
        self.assertIsNone(self.extract_tool_ext("not a dict"))


if __name__ == '__main__':
    unittest.main()
