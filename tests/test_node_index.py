"""Node index: database loading, the Manager/object_info join, search ranking, row shapes, plain functions.

Run with the standalone Python: python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.tools.node_index import (CACHE_TTL, CORE_PACK, CORE_REPO, DB_FILES, DEFAULT_CHANNEL, NodeDatabaseUnavailable,
                                      build_index, get_index, install_guide_row, invalidate_index, load_databases,
                                      node_info, node_row, node_rows_for_types, search_nodes, tokenize)
from backend.utils.comfy_gateway import ComfyTarget, _target, set_target
from fake_comfy import OBJECT_INFO, FakeComfy

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'node_db'
KJ = 'https://github.com/kijai/ComfyUI-KJNodes'
IMPACT = 'https://github.com/ltdrdata/ComfyUI-Impact-Pack'
SOMEONE = 'https://github.com/someone/ComfyUI-SomeoneUtils'
ACME_REF = 'https://github.com/acme/ComfyUI-AcmeTiled-old'
NODE_FIELDS = ['name', 'description', 'image', 'github_url', 'github_stars', 'from_index', 'to_index']


def fixture_fetch(url: str) -> bytes:
    return (FIXTURES / url.rsplit('/', 1)[-1]).read_bytes()


def failing_fetch(url: str) -> bytes:
    raise OSError(f'offline: {url}')


def fixture_databases():
    with patch.dict(os.environ, {'COPILOT_NODE_DB_DIR': str(FIXTURES)}):
        return load_databases(fetch=failing_fetch)


class LoadDatabasesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='copilot-node-index-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_load_databases_prefers_manager_files_then_cache_then_fetch(self):
        empty = self.tmp / 'no-manager'
        cache = self.tmp / 'cache'
        empty.mkdir()
        with patch.dict(os.environ, {'COPILOT_NODE_DB_DIR': str(FIXTURES), 'COPILOT_NODE_INDEX_HOME': str(cache)}):
            db = load_databases(fetch=failing_fetch)
        self.assertEqual(db.source, 'manager')
        self.assertEqual(len(db.custom_nodes), 4)
        self.assertIn('ImageResizeKJ', db.node_map[KJ][0])
        self.assertEqual(db.node_map[KJ][1], {'title_aux': 'KJNodes for ComfyUI'})
        self.assertEqual(db.stats[KJ]['stars'], 3285)
        self.assertEqual([Path(p).name for p in db.paths], list(DB_FILES))
        self.assertFalse(cache.exists())

        urls = []

        def fetch(url):
            urls.append(url)
            return fixture_fetch(url)

        with patch.dict(os.environ, {'COPILOT_NODE_DB_DIR': str(empty), 'COPILOT_NODE_INDEX_HOME': str(cache)}):
            db = load_databases(fetch=fetch)
            self.assertEqual(db.source, 'fetched')
            self.assertEqual(urls, [f'{DEFAULT_CHANNEL}/{name}' for name in DB_FILES])
            self.assertEqual(sorted(p.name for p in cache.iterdir()), sorted(DB_FILES))
            self.assertEqual(len(db.custom_nodes), 4)

            db = load_databases(fetch=failing_fetch)
            self.assertEqual(db.source, 'cache')
            self.assertEqual(db.stats[KJ]['stars'], 3285)

            later = time.time() + CACHE_TTL + 60
            db = load_databases(fetch=failing_fetch, now=lambda: later)
            self.assertEqual(db.source, 'stale')

            urls.clear()
            db = load_databases(fetch=fetch, now=lambda: later)
            self.assertEqual((db.source, len(urls)), ('fetched', 3))

        no_cache = self.tmp / 'no-cache'
        with patch.dict(os.environ, {'COPILOT_NODE_DB_DIR': str(empty), 'COPILOT_NODE_INDEX_HOME': str(no_cache)}):
            with self.assertRaises(NodeDatabaseUnavailable):
                load_databases(fetch=failing_fetch)
            self.assertFalse((no_cache / 'custom-node-list.json').exists())

            def no_stats(url):
                if url.endswith('github-stats.json'):
                    raise OSError('404')
                return fixture_fetch(url)

            db = load_databases(fetch=no_stats)
            self.assertEqual((db.source, db.stats), ('fetched', {}))
            self.assertIsNone(build_index(db, {}).records['ColorMatch'].pack.stars)


class BuildIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = fixture_databases()

    def test_tokenize(self):
        self.assertEqual(tokenize('VAEDecodeTiled'), ['vae', 'decode', 'tiled'])
        self.assertEqual(tokenize('ImageResizeKJ'), ['image', 'resize', 'kj'])
        self.assertEqual(tokenize('VRAM_Debug (v2)'), ['vram', 'debug', 'v2'])
        self.assertEqual(tokenize('KSampler'), ['sampler'])       # 'k' is shorter than 2 chars
        self.assertEqual(tokenize(''), [])

    def test_build_index_joins(self):
        object_info = dict(OBJECT_INFO)
        object_info['AcmeSharpen'] = {'input': {'required': {}}, 'output': ['IMAGE'], 'output_name': ['IMAGE'],
                                      'name': 'AcmeSharpen', 'display_name': 'Acme Sharpen', 'category': 'acme',
                                      'python_module': 'custom_nodes.some-unrelated-folder'}
        object_info['MysteryNode'] = {'input': {'required': {}}, 'output': [], 'name': 'MysteryNode',
                                      'display_name': 'Mystery', 'category': 'misc',
                                      'python_module': 'custom_nodes.mystery-nodes.sub'}
        index = build_index(self.db, object_info)
        records = index.records

        core = records['VAEDecode']
        self.assertTrue(core.installed)
        self.assertIs(core.pack, CORE_PACK)
        self.assertEqual(core.pack.reference, CORE_REPO)
        self.assertEqual((core.display_name, core.category, core.inputs, core.outputs),
                         ('VAE Decode', 'latent', ['samples', 'vae'], ['IMAGE']))

        color = records['ColorMatch']
        self.assertFalse(color.installed)
        self.assertEqual((color.pack.reference, color.pack.stars, color.pack.cnr_id, color.display_name),
                         (KJ, 3285, 'kjnodes', 'ColorMatch'))

        acme = records['AcmeTiledUpscale']                     # node map keyed by files[0], stats by reference
        self.assertEqual((acme.pack.reference, acme.pack.stars, acme.pack.pip, acme.pack.title),
                         (ACME_REF, 12, ('numpy',), 'Acme Tiled Tools'))

        resize = records['ImageResizeKJ']                      # duplicate: installed pack wins
        self.assertTrue(resize.installed)
        self.assertEqual(resize.pack.reference, KJ)
        self.assertEqual(resize.other_packs, [SOMEONE])
        self.assertEqual((resize.display_name, resize.search_aliases, resize.inputs[-1], resize.outputs),
                         ('Resize Image', ['scale image', 'resize'], 'get_image_size', ['IMAGE', 'width', 'height']))
        self.assertIsNone(records['SomeoneTextConcat'].pack.stars)

        logger = records['ImpactLogger']                       # unmapped installed class -> pack by folder
        self.assertTrue(logger.installed)
        self.assertEqual((logger.pack.reference, logger.other_packs), (IMPACT, []))
        self.assertEqual(records['SAMLoader'].pack.reference, IMPACT)

        self.assertEqual(records['AcmeSharpen'].pack.reference, ACME_REF)    # nodename_pattern '^Acme'
        mystery = records['MysteryNode']
        self.assertEqual((mystery.pack.reference, mystery.pack.title, mystery.pack.stars), ('', 'mystery-nodes', None))
        self.assertEqual(records['Int to Text'].pack.title, 'alkemann nodes')  # synthesized from title_aux
        self.assertEqual(index.installed_count, len(object_info))

        # No object_info: the duplicate goes to the pack with the most stars.
        offline = build_index(self.db, {})
        self.assertEqual(offline.installed_count, 0)
        self.assertEqual(offline.records['ImageResizeKJ'].pack.reference, KJ)
        self.assertEqual(offline.records['ImageResizeKJ'].other_packs, [SOMEONE])
        # The installed pack wins even with fewer stars.
        other = build_index(self.db, {'ImageResizeKJ': {**OBJECT_INFO['ImageResizeKJ'],
                                                        'python_module': 'custom_nodes.ComfyUI-SomeoneUtils'}})
        self.assertEqual(other.records['ImageResizeKJ'].pack.reference, SOMEONE)
        self.assertEqual(other.records['ImageResizeKJ'].other_packs, [KJ])

    def test_search_ranking(self):
        index = build_index(self.db, OBJECT_INFO)

        def names(query, **kwargs):
            return [record.class_name for record, _ in index.search(query, **kwargs)]

        self.assertEqual(names('VAEDecodeTiled')[0], 'VAEDecodeTiled')
        self.assertEqual(names('vae decode (tiled)')[0], 'VAEDecodeTiled')       # display name, case-insensitive
        ranked = names('tiled vae decode')
        self.assertIn('VAEDecode', ranked)
        self.assertLess(ranked.index('VAEDecodeTiled'), ranked.index('VAEDecode'))
        self.assertEqual(names('impact pack')[:1], ['ImpactLogger'])            # installed +2 over the same pack
        self.assertEqual(sorted(names('impact pack')[1:4]), ['FaceDetailer', 'ImpactWildcardProcessor', 'SAMLoader'])
        self.assertEqual(sorted(names('acme tiled tools')[:2]), ['AcmeTiledUpscale', 'AcmeTiledVAEDecode'])
        ranked = names('comfy')                                                 # pack-title tie: installed first
        self.assertEqual(ranked[0], 'ImageResizeKJ')
        self.assertLess(ranked.index('ImageResizeKJ'), ranked.index('ColorMatch'))
        self.assertEqual(names('scale')[0], 'ImageResizeKJ')                    # search alias
        self.assertEqual(len(names('image', limit=2)), 2)
        installed_only = index.search('image', installed_only=True)
        self.assertTrue(installed_only and all(record.installed for record, _ in installed_only))
        self.assertNotIn('GetImageSizeAndCount', [r.class_name for r, _ in installed_only])
        self.assertIn('GetImageSizeAndCount', names('image'))
        self.assertEqual(index.search(''), [])
        self.assertEqual(index.search('   '), [])
        self.assertEqual(names('zzzznothing'), [])
        scores = [value for _, value in index.search('image')]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_get_and_suggest(self):
        index = build_index(self.db, OBJECT_INFO)
        self.assertIs(index.get('vaedecodetiled'), index.records['VAEDecodeTiled'])
        self.assertIsNone(index.get('VAEDecodeTile'))
        self.assertEqual(index.suggest('VAEDecodeTile')[0], 'VAEDecodeTiled')
        self.assertIn('VAEDecodeTiled', index.suggest('Tiled'))
        self.assertEqual(index.suggest(''), [])

    def test_node_row_shape(self):
        index = build_index(self.db, {**OBJECT_INFO, 'MysteryNode': {
            'input': {'required': {}}, 'output': [], 'name': 'MysteryNode', 'display_name': 'Mystery', 'category': 'misc',
            'python_module': 'custom_nodes.mystery-nodes'}})
        row = node_row(index.records['ImageResizeKJ'])
        self.assertEqual(list(row)[:7], NODE_FIELDS)
        self.assertEqual([type(row[k]) for k in NODE_FIELDS], [str, str, str, str, int, int, int])
        self.assertEqual((row['name'], row['github_url'], row['github_stars'], row['image']), ('ImageResizeKJ', KJ, 3285, ''))
        self.assertEqual((row['from_index'], row['to_index']), (0, 0))
        self.assertEqual(row['description'], 'Resizes the image to the specified width and height.')
        self.assertEqual((row['installed'], row['display_name'], row['pack_title']), (True, 'Resize Image', 'KJNodes for ComfyUI'))
        json.dumps(row)

        uninstalled = node_row(index.records['ColorMatch'])
        self.assertFalse(uninstalled['installed'])
        self.assertEqual(uninstalled['description'], index.packs[KJ].description)   # pack text stands in
        self.assertEqual(node_row(index.records['SomeoneTextConcat'])['github_stars'], 0)

        synthesized = node_row(index.records['MysteryNode'])
        self.assertEqual((synthesized['github_url'], synthesized['github_stars'], synthesized['pack_title']), ('', 0, 'mystery-nodes'))
        self.assertEqual(node_row(index.records['VAEDecode'])['github_url'], CORE_REPO)

        self.assertEqual(install_guide_row(index.records['FaceDetailer'], 'FaceDetailer'),
                         {'name': 'FaceDetailer', 'repository_url': IMPACT})
        self.assertEqual(install_guide_row(None, 'Nope'), {'name': 'Nope', 'repository_url': ''})
        self.assertEqual(install_guide_row(index.records['MysteryNode'], 'MysteryNode')['repository_url'], '')


class PlainFunctionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='copilot-node-index-'))
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

    async def test_node_info(self):
        info = await node_info('ImageResizeKJ')
        node = info['node']
        self.assertEqual((node['class_name'], node['installed'], node['python_module']),
                         ('ImageResizeKJ', True, 'custom_nodes.comfyui-kjnodes'))
        required = node['inputs']['required']
        self.assertEqual(required['image'], 'IMAGE')
        self.assertEqual(required['width'], ['INT', {'default': 512, 'min': 0, 'max': 16384, 'step': 8}])
        self.assertEqual(len(required['upscale_method']), 9)
        self.assertEqual(required['upscale_method'][-1], '…(+1)')
        self.assertEqual(required['upscale_method'][:8], OBJECT_INFO['ImageResizeKJ']['input']['required']['upscale_method'][0][:8])
        self.assertEqual(node['inputs']['optional'], {'get_image_size': 'IMAGE'})
        self.assertEqual(node['outputs'], ['IMAGE', 'width', 'height'])
        self.assertEqual((info['pack']['title'], info['pack']['reference'], info['pack']['stars'], info['pack']['cnr_id']),
                         ('KJNodes for ComfyUI', KJ, 3285, 'kjnodes'))
        self.assertEqual(info['other_packs'], [SOMEONE])
        self.assertEqual(info['ext'], [{'type': 'node', 'data': [node_row((await get_index()).records['ImageResizeKJ'])]}])
        self.assertNotIn('install_hint', info)

        info = await node_info('FaceDetailer')
        self.assertFalse(info['node']['installed'])
        self.assertNotIn('inputs', info['node'])
        self.assertNotIn('outputs', info['node'])
        self.assertEqual(info['install_hint'], f'not installed; pack ComfyUI Impact Pack at {IMPACT}')
        self.assertEqual(info['pack']['pip'], [])

        self.assertEqual((await node_info('vaedecode'))['node']['class_name'], 'VAEDecode')   # case-insensitive
        info = await node_info('VAEDecodeTile')
        self.assertIn('not found', info['error'])
        self.assertEqual(info['suggestions'][0], 'VAEDecodeTiled')
        self.assertIn('error', await node_info(''))

    async def test_search_nodes_envelope(self):
        result = await search_nodes('tiled vae decode', limit=3)
        self.assertEqual(result['answer'], "3 nodes matched 'tiled vae decode' (2 installed)")
        self.assertEqual(result['data'][0]['name'], 'VAEDecodeTiled')
        self.assertEqual(result['ext'], [{'type': 'node', 'data': result['data']}])
        self.assertEqual(await search_nodes('  '), {'error': 'query is empty'})

    async def test_node_rows_for_types_order_and_unknowns(self):
        rows = await node_rows_for_types(['FaceDetailer', 'Nope', 'VAEDecode', 'ImageResizeKJ'])
        self.assertEqual([r['name'] for r in rows], ['FaceDetailer', 'Nope', 'VAEDecode', 'ImageResizeKJ'])
        for row in rows:
            self.assertEqual(list(row)[:7], NODE_FIELDS)
        self.assertEqual((rows[0]['installed'], rows[0]['github_url'], rows[0]['github_stars']), (False, IMPACT, 2500))
        self.assertEqual((rows[1]['installed'], rows[1]['github_url'], rows[1]['github_stars'], rows[1]['description']),
                         (False, '', 0, ''))
        self.assertEqual((rows[2]['installed'], rows[2]['github_url']), (True, CORE_REPO))
        self.assertEqual(rows[3]['installed'], True)
        self.assertEqual(await node_rows_for_types([]), [])

    async def test_get_index_uses_fake_object_info_and_caches(self):
        first = await get_index()
        self.assertEqual(self.fake.object_info_calls, 1)
        self.assertEqual((first.db_source, first.installed_count), ('manager', len(OBJECT_INFO)))
        self.assertTrue(first.records['ImageResizeKJ'].installed)
        self.assertIs(await get_index(), first)
        self.assertEqual(self.fake.object_info_calls, 1)

        # A changed DB file rebuilds without a new object_info request.
        listing = json.loads((self.db_dir / 'custom-node-list.json').read_text(encoding='utf-8'))
        listing['custom_nodes'].append({'author': 'x', 'title': 'Late Pack', 'reference': 'https://github.com/x/late',
                                        'files': ['https://github.com/x/late'], 'install_type': 'git-clone', 'description': ''})
        (self.db_dir / 'custom-node-list.json').write_text(json.dumps(listing), encoding='utf-8')
        rebuilt = await get_index()
        self.assertIsNot(rebuilt, first)
        self.assertIn('https://github.com/x/late', rebuilt.packs)
        self.assertEqual(self.fake.object_info_calls, 1)

        invalidate_index()
        forced = await get_index()
        self.assertIsNot(forced, rebuilt)
        self.assertEqual(self.fake.object_info_calls, 2)
        await get_index(force=True)
        self.assertEqual(self.fake.object_info_calls, 3)

        await self.fake.stop()
        invalidate_index()
        offline = await get_index()
        self.assertEqual(offline.installed_count, 0)
        self.assertIn('ImageResizeKJ', offline.records)
        self.assertFalse(offline.records['ImageResizeKJ'].installed)
        self.assertEqual((await node_rows_for_types(['VAEDecode']))[0]['installed'], False)


if __name__ == '__main__':
    unittest.main()
