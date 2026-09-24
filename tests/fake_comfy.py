"""In-process ComfyUI double for the endpoints the gateway uses.

Shapes follow ComfyUI 0.37: /api/prompt validation replies, /api/queue tuples,
and /api/history entries with status.messages as written by PromptQueue.task_done.
"""
import asyncio
import json
import time
import uuid

from aiohttp import web

CUTLASS_ERROR = {
    'exception_type': 'RuntimeError',
    'exception_message': 'cutlass_fp16_linear: K mismatch (expected 2048, got 768)\n',
    'traceback': [
        '  File "D:\\ComfyUI\\execution.py", line 511, in execute\n    output_data, output_ui, has_subgraph, has_pending_tasks = await get_output_data(prompt_id, unique_id, obj, input_data_all, execution_block_cb=execution_block_cb, pre_execute_cb=pre_execute_cb)\n',
        '  File "D:\\ComfyUI\\execution.py", line 290, in get_output_data\n    return_values = await _async_map_node_over_list(prompt_id, unique_id, obj, input_data_all, obj.FUNCTION, allow_interrupt=True, execution_block_cb=execution_block_cb, pre_execute_cb=pre_execute_cb)\n',
        '  File "D:\\ComfyUI\\execution.py", line 264, in _async_map_node_over_list\n    await process_inputs(input_dict, i)\n',
        '  File "D:\\ComfyUI\\execution.py", line 252, in process_inputs\n    result = f(**inputs)\n',
        '  File "D:\\ComfyUI\\nodes.py", line 1516, in sample\n    return common_ksampler(model, seed, steps, cfg, sampler_name, scheduler, positive, negative, latent_image, denoise=denoise)\n',
        '  File "D:\\ComfyUI\\nodes.py", line 1483, in common_ksampler\n    samples = comfy.sample.sample(model, noise, steps, cfg, sampler_name, scheduler, positive, negative, latent_image, denoise=denoise, disable_noise=disable_noise, start_step=start_step, last_step=last_step, force_full_denoise=force_full_denoise, noise_mask=noise_mask, callback=callback, disable_pbar=disable_pbar, seed=seed)\n',
        '  File "D:\\ComfyUI\\comfy\\sample.py", line 45, in sample\n    samples = sampler.sample(noise, positive, negative, cfg=cfg, latent_image=latent_image, start_step=start_step, last_step=last_step, force_full_denoise=force_full_denoise, denoise_mask=noise_mask, sigmas=sigmas, callback=callback, disable_pbar=disable_pbar, seed=seed)\n',
        '  File "D:\\ComfyUI\\comfy\\samplers.py", line 1143, in sample\n    return sample(self.model, noise, positive, negative, cfg, self.device, sampler, sigmas, self.model_options, latent_image=latent_image, denoise_mask=denoise_mask, callback=callback, disable_pbar=disable_pbar, seed=seed)\n',
        '  File "D:\\ComfyUI\\comfy\\samplers.py", line 1033, in sample\n    return cfg_guider.sample(noise, latent_image, sampler, sigmas, denoise_mask, callback, disable_pbar, seed)\n',
        '  File "D:\\ComfyUI\\comfy\\samplers.py", line 1018, in sample\n    output = executor.execute(noise, latent_image, sampler, sigmas, denoise_mask, callback, disable_pbar, seed)\n',
        '  File "D:\\ComfyUI\\comfy\\patcher_extension.py", line 112, in execute\n    return self.original(*args, **kwargs)\n',
        '  File "D:\\ComfyUI\\comfy\\samplers.py", line 986, in outer_sample\n    output = self.inner_sample(noise, latent_image, device, sampler, sigmas, denoise_mask, callback, disable_pbar, seed)\n',
        '  File "D:\\ComfyUI\\comfy\\samplers.py", line 969, in inner_sample\n    samples = executor.execute(self, sigmas, extra_args, callback, noise, latent_image, denoise_mask, disable_pbar)\n',
        '  File "D:\\ComfyUI\\comfy\\samplers.py", line 748, in sample\n    samples = self.sampler_function(model_k, noise, sigmas, extra_args=extra_args, callback=k_callback, disable=disable_pbar, **self.extra_options)\n',
        '  File "D:\\ComfyUI\\comfy\\k_diffusion\\sampling.py", line 161, in sample_euler\n    denoised = model(x, sigma_hat * s_in, **extra_args)\n',
        '  File "D:\\ComfyUI\\comfy\\samplers.py", line 390, in __call__\n    out = self.inner_model(x, sigma, model_options=model_options, seed=seed)\n',
        '  File "D:\\ComfyUI\\comfy\\samplers.py", line 939, in __call__\n    return self.predict_noise(*args, **kwargs)\n',
        '  File "D:\\ComfyUI\\comfy\\ldm\\modules\\diffusionmodules\\openaimodel.py", line 831, in forward\n    h = forward_timestep_embed(module, h, emb, context, transformer_options, time_context=time_context, num_video_frames=num_video_frames, image_only_indicator=image_only_indicator)\n',
        '  File "D:\\ComfyUI\\comfy\\ldm\\modules\\attention.py", line 691, in forward\n    x = block(x, context=context[i], transformer_options=transformer_options)\n',
        '  File "D:\\ComfyUI\\comfy\\ops.py", line 74, in forward_comfy_cast_weights\n    return torch.nn.functional.linear(input, weight, bias)\n',
    ],
}

VALIDATION_ERROR = {
    'type': 'prompt_outputs_failed_validation',
    'message': 'Prompt outputs failed validation',
    'details': '',
    'extra_info': {},
}

OBJECT_INFO = {
    'KSampler': {
        'input': {'required': {
            'model': ['MODEL'], 'seed': ['INT', {'default': 0, 'min': 0, 'max': 18446744073709551615}],
            'steps': ['INT', {'default': 20, 'min': 1, 'max': 10000}], 'cfg': ['FLOAT', {'default': 8.0, 'min': 0.0, 'max': 100.0}],
            'sampler_name': [['euler', 'euler_ancestral', 'dpmpp_2m']], 'scheduler': [['normal', 'karras', 'simple']],
            'positive': ['CONDITIONING'], 'negative': ['CONDITIONING'], 'latent_image': ['LATENT'],
            'denoise': ['FLOAT', {'default': 1.0, 'min': 0.0, 'max': 1.0}]}},
        'output': ['LATENT'], 'output_name': ['LATENT'], 'name': 'KSampler', 'display_name': 'KSampler',
        'category': 'sampling', 'output_node': False, 'python_module': 'nodes',
    },
    'VAEDecode': {
        'input': {'required': {'samples': ['LATENT'], 'vae': ['VAE']}},
        'output': ['IMAGE'], 'output_name': ['IMAGE'], 'name': 'VAEDecode', 'display_name': 'VAE Decode',
        'category': 'latent', 'output_node': False, 'python_module': 'nodes',
    },
    'VAEDecodeTiled': {
        'input': {'required': {
            'samples': ['LATENT'], 'vae': ['VAE'], 'tile_size': ['INT', {'default': 512, 'min': 64, 'max': 4096, 'step': 32}],
            'overlap': ['INT', {'default': 64, 'min': 0, 'max': 4096, 'step': 32}],
            'temporal_size': ['INT', {'default': 64, 'min': 8, 'max': 4096, 'step': 4}],
            'temporal_overlap': ['INT', {'default': 8, 'min': 4, 'max': 4096, 'step': 4}]}},
        'output': ['IMAGE'], 'output_name': ['IMAGE'], 'name': 'VAEDecodeTiled', 'display_name': 'VAE Decode (Tiled)',
        'category': '_for_testing', 'output_node': False, 'python_module': 'nodes',
    },
    'EmptyLatentImage': {
        'input': {'required': {
            'width': ['INT', {'default': 512, 'min': 16, 'max': 16384, 'step': 8}],
            'height': ['INT', {'default': 512, 'min': 16, 'max': 16384, 'step': 8}],
            'batch_size': ['INT', {'default': 1, 'min': 1, 'max': 4096}]}},
        'output': ['LATENT'], 'output_name': ['LATENT'], 'name': 'EmptyLatentImage', 'display_name': 'Empty Latent Image',
        'category': 'latent', 'output_node': False, 'python_module': 'nodes',
    },
    # Installed custom node that the tests/fixtures/node_db map attributes to KJNodes.
    'ImageResizeKJ': {
        'input': {'required': {
            'image': ['IMAGE'], 'width': ['INT', {'default': 512, 'min': 0, 'max': 16384, 'step': 8}],
            'height': ['INT', {'default': 512, 'min': 0, 'max': 16384, 'step': 8}],
            'upscale_method': [['nearest-exact', 'bilinear', 'area', 'bicubic', 'lanczos', 'box', 'hamming', 'hann', 'blackman']],
            'keep_proportion': ['BOOLEAN', {'default': False}]},
            'optional': {'get_image_size': ['IMAGE']}},
        'output': ['IMAGE', 'INT', 'INT'], 'output_name': ['IMAGE', 'width', 'height'], 'name': 'ImageResizeKJ',
        'display_name': 'Resize Image', 'description': 'Resizes the image to the specified width and height.',
        'category': 'KJNodes/image', 'output_node': False, 'python_module': 'custom_nodes.comfyui-kjnodes',
        'search_aliases': ['scale image', 'resize'],
    },
    # Installed custom node absent from the fixture map; its folder matches the Impact Pack reference.
    'ImpactLogger': {
        'input': {'required': {'data': ['*'], 'text': ['STRING', {'default': ''}]}},
        'output': [], 'output_name': [], 'name': 'ImpactLogger', 'display_name': 'Logger (Impact)',
        'category': 'ImpactPack/Debug', 'output_node': True, 'python_module': 'custom_nodes.comfyui-impact-pack',
    },
}

SYSTEM_STATS = {
    'system': {
        'os': 'win32', 'ram_total': 34_000_000_000, 'ram_free': 20_000_000_000, 'comfyui_version': '0.37.0',
        'python_version': '3.13.14', 'pytorch_version': '2.9.0+cu130', 'embedded_python': True,
        'argv': ['ComfyUI\\main.py', '--windows-standalone-build'],
    },
    'devices': [{
        'name': 'cuda:0 NVIDIA GeForce RTX 4070 : cudaMallocAsync', 'type': 'cuda', 'index': 0,
        'vram_total': 8_585_216_000, 'vram_free': 7_300_000_000, 'torch_vram_total': 0, 'torch_vram_free': 0,
    }],
}


class FakeComfy:
    """aiohttp double for the ComfyUI endpoints the gateway uses."""

    def __init__(self, *, node_errors=None, execution='success', run_seconds=0.05, validate_route=True,
                 error=CUTLASS_ERROR, error_node=('9', 'KSampler')):
        # node_errors: dict -> /api/prompt and /api/copilot/validate report validation failure
        # execution: 'success' | 'error' | 'interrupted' | 'hang'
        self.node_errors = node_errors or {}
        self.execution = execution
        self.run_seconds = run_seconds
        self.validate_route = validate_route
        self.error = error
        self.error_node = error_node
        self.prompts = []
        self.interrupts = []
        self.deleted = []
        self.history = {}
        self.object_info_calls = 0
        self.pending = set()
        self.running = set()
        self.base_url = None
        self._items = {}
        self._queue = asyncio.Queue()
        self._interrupt = asyncio.Event()
        self._worker = None
        self._runner = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post('/api/prompt', self.post_prompt)
        app.router.add_get('/api/history/{prompt_id}', self.get_history)
        app.router.add_get('/api/queue', self.get_queue)
        app.router.add_post('/api/queue', self.post_queue)
        app.router.add_post('/api/interrupt', self.post_interrupt)
        app.router.add_get('/api/object_info', self.object_info)
        app.router.add_get('/api/object_info/{cls}', self.object_info)
        app.router.add_get('/api/system_stats', self.system_stats)
        if self.validate_route:
            app.router.add_post('/api/copilot/validate', self.validate)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base_url = f'http://127.0.0.1:{port}'
        self._worker = asyncio.create_task(self._work())
        return self.base_url

    async def stop(self):
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if self._runner:
            await self._runner.cleanup()

    # -- routes ---------------------------------------------------------------

    async def post_prompt(self, request):
        body = await request.json()
        self.prompts.append(body)
        if self.node_errors:
            return web.json_response({'error': VALIDATION_ERROR, 'node_errors': self.node_errors}, status=400)
        prompt_id = str(uuid.uuid4())
        prompt = body.get('prompt', {})
        outputs = [k for k, v in prompt.items() if v.get('class_type') in ('SaveImage', 'PreviewImage')] or ['9']
        self._items[prompt_id] = [len(self.prompts), prompt_id, prompt, {'client_id': body.get('client_id')}, outputs]
        self.pending.add(prompt_id)
        self._queue.put_nowait(prompt_id)
        return web.json_response({'prompt_id': prompt_id, 'number': len(self.prompts), 'node_errors': {}})

    async def get_history(self, request):
        prompt_id = request.match_info['prompt_id']
        return web.json_response({prompt_id: self.history[prompt_id]} if prompt_id in self.history else {})

    async def get_queue(self, request):
        return web.json_response({'queue_running': [self._items[p] for p in self.running],
                                  'queue_pending': [self._items[p] for p in self.pending]})

    async def post_queue(self, request):
        body = await request.json()
        if body.get('clear'):
            self.deleted.extend(sorted(self.pending))
            self.pending.clear()
        for prompt_id in body.get('delete', []):
            self.deleted.append(prompt_id)
            self.pending.discard(prompt_id)   # a running prompt is untouched, like delete_queue_item
        return web.Response(status=200)

    async def post_interrupt(self, request):
        text = await request.text()
        prompt_id = (json.loads(text) if text else {}).get('prompt_id')
        self.interrupts.append(prompt_id)
        if prompt_id is None or prompt_id in self.running:
            self._interrupt.set()
        return web.Response(status=200)

    async def object_info(self, request):
        cls = request.match_info.get('cls')
        self.object_info_calls += 1
        if cls is None:
            return web.json_response(OBJECT_INFO)
        return web.json_response({cls: OBJECT_INFO[cls]} if cls in OBJECT_INFO else {})

    async def system_stats(self, request):
        return web.json_response(SYSTEM_STATS)

    async def validate(self, request):
        prompt = (await request.json())['prompt']
        outputs = [k for k, v in prompt.items() if v.get('class_type') in ('SaveImage', 'PreviewImage')]
        if self.node_errors:
            return web.json_response({'valid': False, 'error': VALIDATION_ERROR, 'node_errors': self.node_errors, 'outputs': []})
        return web.json_response({'valid': True, 'error': None, 'node_errors': {}, 'outputs': outputs})

    # -- execution ------------------------------------------------------------

    async def _work(self):
        # One prompt at a time, like PromptQueue; a deleted pending id is skipped.
        while True:
            prompt_id = await self._queue.get()
            if prompt_id in self.pending:
                await self._run(prompt_id)

    async def _run(self, prompt_id):
        self.pending.discard(prompt_id)
        self.running.add(prompt_id)
        self._interrupt.clear()
        item = self._items[prompt_id]
        messages = [self._message('execution_start', {'prompt_id': prompt_id})]
        outcome = self.execution
        try:
            await asyncio.wait_for(self._interrupt.wait(), None if outcome == 'hang' else self.run_seconds)
            outcome = 'interrupted'
        except asyncio.TimeoutError:
            pass
        node_id, node_type = self.error_node
        executed = [k for k in item[2] if k != node_id]
        outputs = {}
        if outcome == 'error':
            messages.append(self._message('execution_error', {
                'prompt_id': prompt_id, 'node_id': node_id, 'node_type': node_type, 'executed': executed,
                'exception_message': self.error['exception_message'], 'exception_type': self.error['exception_type'],
                'traceback': self.error['traceback'], 'current_inputs': {'seed': [0], 'steps': [20]},
                'current_outputs': executed}))
        elif outcome == 'interrupted':
            messages.append(self._message('execution_interrupted', {
                'prompt_id': prompt_id, 'node_id': node_id, 'node_type': node_type, 'executed': executed}))
        else:
            messages.append(self._message('execution_success', {'prompt_id': prompt_id}))
            outputs = {out: {'images': [{'filename': f'ComfyUI_{len(self.history) + 1:05d}_.png', 'subfolder': '', 'type': 'output'}]}
                       for out in item[4]}
        success = outcome not in ('error', 'interrupted')
        self.running.discard(prompt_id)
        self.history[prompt_id] = {
            'prompt': item, 'outputs': outputs,
            'status': {'status_str': 'success' if success else 'error', 'completed': success, 'messages': messages},
            'meta': {out: {'node_id': out, 'display_node': out, 'parent_node': None, 'real_node_id': out} for out in outputs},
        }

    @staticmethod
    def _message(event, data):
        return [event, {**data, 'timestamp': int(time.time() * 1000)}]
