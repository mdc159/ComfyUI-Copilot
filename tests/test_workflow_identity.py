"""Workflow identity (roadmap phase 1b): a debug/rewrite run stays pinned to the workflow
version it started on, instead of "latest row for this session" which any other browser tab
sharing the same session_id can clobber.

Run with the standalone Python: python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backend.dao.workflow_table as workflow_table
from backend.utils.request_context import (
    clear_request_context, get_workflow_checkpoint_id, set_request_context,
)


class TempWorkflowDbMixin:
    """Points backend.dao.workflow_table.db_manager at a throwaway sqlite file for the test.

    The module-level ``db_manager`` singleton defaults to backend/data/workflow_debug.db;
    every convenience function (get_workflow_data, save_workflow_data, ...) looks it up from
    the module namespace at call time, so swapping it here is enough to isolate every caller
    (workflow_rewrite_tools, parameter_tools, graph_edit, debug_agent) without patching each one.
    """

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(self._tmpdir, 'workflow_debug_test.db')
        self._original_db_manager = workflow_table.db_manager
        workflow_table.db_manager = workflow_table.DatabaseManager(db_path)
        clear_request_context()

    def tearDown(self):
        workflow_table.db_manager = self._original_db_manager
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        clear_request_context()
        super().tearDown()


class ResolverPrefersCheckpointTests(TempWorkflowDbMixin, unittest.TestCase):
    def test_get_workflow_data_from_config_prefers_checkpoint(self):
        from backend.dao.workflow_table import save_workflow_data
        from backend.service.workflow_rewrite_tools import get_workflow_data_from_config

        checkpoint_a = save_workflow_data('session-1', {'tabA': True}, attributes={'workflow_key': 'tabA'})
        save_workflow_data('session-1', {'tabB': True}, attributes={'workflow_key': 'tabB'})

        config = {'session_id': 'session-1', 'workflow_checkpoint_id': checkpoint_a, 'workflow_key': 'tabA'}
        self.assertEqual(get_workflow_data_from_config(config), {'tabA': True})


class GetCurrentWorkflowIgnoresOtherKeyTests(TempWorkflowDbMixin, unittest.TestCase):
    def test_get_current_workflow_ignores_newer_row_for_another_key(self):
        from backend.dao.workflow_table import save_workflow_data
        from backend.service.workflow_rewrite_tools import get_current_workflow

        checkpoint_a = save_workflow_data('session-2', {'tabA': True}, attributes={'workflow_key': 'tabA'})
        config = {'session_id': 'session-2', 'workflow_checkpoint_id': checkpoint_a, 'workflow_key': 'tabA'}
        set_request_context('session-2', checkpoint_a, config)

        # Simulate another browser tab (same session_id) saving a newer version under its own key.
        save_workflow_data('session-2', {'tabB': True}, attributes={'workflow_key': 'tabB'})

        result = json.loads(get_current_workflow.__wrapped__())
        self.assertEqual(result, {'tabA': True})


class ToolWriteRepinsContextTests(TempWorkflowDbMixin, unittest.TestCase):
    def test_update_workflow_parameter_repins_context_to_new_version(self):
        from backend.dao.workflow_table import save_workflow_data, get_workflow_data_by_id
        from backend.service.parameter_tools import update_workflow_parameter

        workflow = {'4': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'old.safetensors'}}}
        checkpoint_a = save_workflow_data(
            'session-3', workflow, attributes={'workflow_key': 'tabA', 'workflow_hash': 'hash-1'}
        )
        config = {
            'session_id': 'session-3', 'workflow_checkpoint_id': checkpoint_a,
            'workflow_key': 'tabA', 'workflow_hash': 'hash-1',
        }
        set_request_context('session-3', checkpoint_a, config)

        result = json.loads(update_workflow_parameter.__wrapped__('4', 'ckpt_name', 'new.safetensors'))
        self.assertTrue(result.get('success'))

        new_checkpoint_id = get_workflow_checkpoint_id()
        self.assertIsNotNone(new_checkpoint_id)
        self.assertNotEqual(new_checkpoint_id, checkpoint_a)

        new_version = get_workflow_data_by_id(new_checkpoint_id)
        self.assertEqual(new_version['workflow_data']['4']['inputs']['ckpt_name'], 'new.safetensors')
        # The write carried the same workflow_key/workflow_hash forward.
        self.assertEqual(new_version['attributes']['workflow_key'], 'tabA')
        self.assertEqual(new_version['attributes']['workflow_hash'], 'hash-1')

        # The ext the frontend applies also carries the identity, so a mismatched tab can be
        # detected client-side.
        ext = result['ext'][0]
        self.assertEqual(ext['type'], 'param_update')
        self.assertEqual(ext['data']['workflow_key'], 'tabA')
        self.assertEqual(ext['data']['workflow_hash'], 'hash-1')


class DebugRunPinsBeforeFirstToolTests(TempWorkflowDbMixin, unittest.IsolatedAsyncioTestCase):
    async def test_debug_run_pins_checkpoint_before_creating_the_coordinator_agent(self):
        from backend.service import debug_agent
        from backend.dao.workflow_table import get_workflow_data_by_id

        captured = {}

        def fake_create_agent(**kwargs):
            # This runs before Runner.run_streamed / any tool call, so if the checkpoint is
            # already pinned here it is certainly pinned before the first tool executes.
            captured['checkpoint_id'] = get_workflow_checkpoint_id()
            raise RuntimeError('stop before Runner.run_streamed; only checking the pin')

        set_request_context('session-4', None, {
            'session_id': 'session-4', 'workflow_key': 'tabA', 'workflow_hash': 'hash-1',
        })

        workflow_data = {'1': {'class_type': 'KSampler', 'inputs': {}}}
        with mock.patch.object(debug_agent, 'create_agent', fake_create_agent):
            results = [item async for item in debug_agent.debug_workflow_errors(workflow_data)]

        self.assertTrue(results)  # the error path still yields something instead of raising
        self.assertIn('checkpoint_id', captured)
        self.assertIsNotNone(captured['checkpoint_id'])

        pinned = get_workflow_data_by_id(captured['checkpoint_id'])
        self.assertEqual(pinned['workflow_data'], workflow_data)
        self.assertEqual(pinned['attributes']['workflow_key'], 'tabA')
        self.assertEqual(pinned['attributes']['workflow_hash'], 'hash-1')


if __name__ == '__main__':
    unittest.main()
