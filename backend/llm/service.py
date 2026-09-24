import asyncio
from collections import deque
import copy
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from urllib.parse import urlparse
import uuid

log = logging.getLogger('comfyui_copilot')
RUNTIME = Path(__file__).resolve().parents[2] / 'llm-runtime' / 'provider.mjs'
API_PRESETS = {
    'openai': ('OpenAI API', 'OPENAI_API_KEY'),
    'openrouter': ('OpenRouter', 'OPENROUTER_API_KEY'),
    'anthropic': ('Anthropic API', 'ANTHROPIC_API_KEY'),
    'google': ('Google Gemini API', 'GEMINI_API_KEY'),
    'groq': ('Groq', 'GROQ_API_KEY'),
    'xai': ('xAI API', 'XAI_API_KEY'),
    'kimi-coding': ('Kimi Coding API key', 'KIMI_API_KEY'),
}
OAUTH_PRESETS = {
    'openai-codex': 'ChatGPT subscription', 'anthropic': 'Claude subscription',
    'github-copilot': 'GitHub Copilot', 'kimi-coding': 'Kimi Coding sign-in',
    'openrouter': 'OpenRouter sign-in', 'xai': 'xAI sign-in',
}


def state_root():
    if os.getenv('COPILOT_LLM_HOME'):
        return Path(os.environ['COPILOT_LLM_HOME'])
    import folder_paths
    return Path(folder_paths.get_user_directory()) / 'copilot-llm'


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def redact(text, request):
    """Remove credential values from runtime diagnostics before they reach the log."""
    secrets = list((request.get('credential') or {}).values())
    if request.get('envVar'):
        secrets.append(os.getenv(request['envVar']))
    for value in secrets:
        if isinstance(value, str) and len(value) > 8:
            text = text.replace(value, '[redacted]')
    return text


class CredentialStore:
    """Windows DPAPI encrypts credentials for the current OS user."""
    def __init__(self, root):
        self.root = root

    def read(self, connection_id):
        path = self.root / (connection_id + '.bin')
        if not path.exists():
            return None
        if os.name != 'nt':
            raise ValueError('Saved credentials require the Windows credential store on this installation')
        import win32crypt
        raw = win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)[1]
        return json.loads(raw)

    def write(self, connection_id, value):
        if os.name != 'nt':
            raise ValueError('Saved credentials require the Windows credential store on this installation')
        import win32crypt
        self.root.mkdir(parents=True, exist_ok=True)
        protected = win32crypt.CryptProtectData(json.dumps(value).encode(), 'Copilot model credential', None, None, None, 0)
        path = self.root / (connection_id + '.bin')
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(protected)
        os.replace(temporary, path)

    def delete(self, connection_id):
        (self.root / (connection_id + '.bin')).unlink(missing_ok=True)


class ModelService:
    def __init__(self, root=None):
        self.root = Path(root) if root else state_root()
        self.credentials = CredentialStore(self.root / 'credentials')
        self.lock = threading.RLock()
        self.connection_locks = {}
        self.signins = {}
        self.catalogs = {}
        path = self.root / 'connections.json'
        if path.exists():
            self.config = json.loads(path.read_text(encoding='utf-8'))
        else:
            connections = [{ 'id': provider, 'name': name, 'provider': provider, 'mode': 'api', 'env': env }
                           for provider, (name, env) in API_PRESETS.items()]
            connections += [{ 'id': provider + '-login', 'name': name, 'provider': provider, 'mode': 'oauth' }
                            for provider, name in OAUTH_PRESETS.items()]
            connections += [
                {'id': 'lmstudio', 'name': 'LM Studio', 'provider': 'custom', 'mode': 'local', 'base_url': 'http://127.0.0.1:1234/v1'},
                {'id': 'ollama', 'name': 'Ollama', 'provider': 'custom', 'mode': 'local', 'base_url': 'http://127.0.0.1:11434/v1'},
            ]
            self.config = {'connections': connections, 'chat': {'connection': 'openai', 'model': 'gpt-4.1'}, 'workflow': None}

    def connection(self, connection_id):
        for connection in self.config['connections']:
            if connection['id'] == connection_id:
                return dict(connection)
        raise ValueError('Unknown model connection')

    def connection_lock(self, connection_id):
        with self.lock:
            return self.connection_locks.setdefault(connection_id, threading.Lock())

    def persist(self):
        atomic_json(self.root / 'connections.json', self.config)

    def status(self):
        with self.lock:
            result = copy.deepcopy(self.config)
        for c in result['connections']:
            credential = self.credentials.read(c['id'])
            c['configured'] = bool(credential or (c.get('env') and os.getenv(c['env'])) or c['mode'] == 'local')
            c['credential_source'] = 'encrypted store' if credential else ('environment' if c.get('env') and os.getenv(c['env']) else ('none required' if c['mode'] == 'local' else 'not connected'))
        return result

    def save_connection(self, data):
        connection_id = data.get('id') or ('custom-' + uuid.uuid4().hex[:10])
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', connection_id):
            raise ValueError('Invalid connection ID')
        old = next((c for c in self.config['connections'] if c['id'] == connection_id), None)
        c = dict(old) if old else {'id': connection_id, 'provider': 'custom', 'mode': 'api'}
        if c['mode'] == 'oauth':
            raise ValueError('Use the provider sign-in for this connection')
        c['name'] = str(data.get('name') or c.get('name') or 'Custom endpoint')[:100]
        if 'base_url' in data:
            url = str(data['base_url']).rstrip('/')
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Provide an HTTP(S) endpoint without embedded credentials')
            if c['provider'] != 'custom':
                raise ValueError('Create a custom connection to change the provider endpoint')
            c['base_url'] = url
        if c['provider'] == 'custom' and not c.get('base_url'):
            raise ValueError('A custom connection requires a base URL')
        if 'env' in data:
            name = str(data['env']).strip()
            if name and not re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', name):
                raise ValueError('Invalid environment variable name')
            c['env'] = name
        with self.connection_lock(connection_id):
            if data.get('api_key'):
                self.credentials.write(connection_id, {'type': 'api_key', 'key': data['api_key']})
            with self.lock:
                self.config['connections'] = [c if x['id'] == connection_id else x for x in self.config['connections']]
                if not old:
                    self.config['connections'].append(c)
                self.catalogs.pop(connection_id, None)
                self.persist()
        return connection_id

    def save_defaults(self, chat, workflow):
        for selection in (chat, workflow):
            if selection is not None:
                self.connection(selection.get('connection'))
                if not isinstance(selection.get('model'), str) or not selection['model'].strip():
                    raise ValueError('Choose a model for each selected connection')
        if chat is None:
            raise ValueError('Choose a chat connection and model')
        with self.lock:
            self.config['chat'] = {'connection': chat['connection'], 'model': chat['model']}
            self.config['workflow'] = {'connection': workflow['connection'], 'model': workflow['model']} if workflow else None
            self.persist()

    def selection(self, config=None):
        config = config or {}
        role = config.get('llm_role', 'chat')
        selected = dict((self.config.get('workflow') if role == 'workflow' else None) or self.config['chat'])
        if role == 'chat' and config.get('model_select'):
            selected['model'] = config['model_select']
        return selected

    def _request(self, connection_id, payload):
        c = self.connection(connection_id)
        credential = self.credentials.read(connection_id)
        env_name = c.get('env')
        if c['mode'] == 'oauth' and (not credential or credential.get('type') != 'oauth') and payload['action'] not in ('login', 'catalog'):
            raise ValueError('Sign in to the selected subscription connection. API billing fallback is disabled.')
        if not credential and env_name and os.getenv(env_name):
            credential = {'type': 'api_key', 'key': os.environ[env_name]}
        request = {**payload, 'provider': c['provider'], 'credential': credential}
        if c.get('base_url'):
            request['baseUrl'] = c['base_url']
        node = shutil.which('node')
        if not node:
            raise ValueError('Node.js 22.19 or newer is required for model connections')
        command = [node, str(RUNTIME)]
        needs_key = payload['action'] == 'complete' or (payload['action'] == 'catalog' and c.get('base_url'))
        if c['mode'] == 'api' and not credential and needs_key:
            if env_name and shutil.which('latchkey'):
                command = [shutil.which('latchkey'), 'run', '--keys', env_name, '--', *command]
                request['envVar'] = env_name
            else:
                raise ValueError('Configure an API key or environment variable for this connection')
        return command, request

    async def events(self, connection_id, payload, signin=None):
        guard = self.connection_lock(connection_id)
        # Poll without parking a thread that could acquire a lock after cancellation.
        while not guard.acquire(blocking=False):
            await asyncio.sleep(0.05)
        process = None
        stderr_task = None
        try:
            command, request = self._request(connection_id, payload)
            process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            # Drain stderr continuously so a noisy runtime cannot block on a full pipe.
            stderr_tail = deque(maxlen=40)
            async def drain():
                async for raw in process.stderr:
                    stderr_tail.append(raw.decode('utf-8', 'replace').rstrip())
            stderr_task = asyncio.create_task(drain())
            if signin is not None:
                signin['process'] = process
            process.stdin.write((json.dumps(request) + '\n').encode())
            await process.stdin.drain()
            result_seen = False
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), 920 if signin is not None else 330)
                if not line:
                    break
                event = json.loads(line)
                if event['type'] == 'credential':
                    self.credentials.write(connection_id, event['value'])
                elif event['type'] == 'error':
                    raise ValueError(event['message'])
                else:
                    if event['type'] == 'result':
                        result_seen = True
                    yield event
            if not result_seen:
                await process.wait()
                try:
                    await asyncio.wait_for(stderr_task, 2)
                except asyncio.TimeoutError:
                    pass
                log.warning('Model runtime exited with code %s before a result. stderr tail:\n%s',
                            process.returncode, redact('\n'.join(stderr_tail), request))
                raise ValueError('Model provider exited before completing the request')
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            if stderr_task and not stderr_task.done():
                stderr_task.cancel()
            guard.release()

    async def call(self, connection_id, payload):
        result = None
        async for event in self.events(connection_id, payload):
            if event['type'] == 'result':
                result = event['value']
        return result

    def complete_sync(self, config, messages, schema=None, **options):
        selected = self.selection(config)
        # Synchronous upstream helper. Use a separate loop/thread if called by an async tool.
        def run():
            return asyncio.run(self.call(selected['connection'], {'action': 'complete',
                'model': selected['model'], 'messages': messages, 'schema': schema, **options}))
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return run()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(run).result()

    async def catalog(self, connection_id):
        # A catalog is not proof of a valid credential or model entitlement.
        rows = await self.call(connection_id, {'action': 'catalog'})
        self.catalogs[connection_id] = rows
        return rows

    async def start_login(self, connection_id):
        c = self.connection(connection_id)
        if c['mode'] != 'oauth':
            raise ValueError('This connection uses API credentials')
        for state in self.signins.values():
            if state['connection'] == connection_id and state['status'] == 'pending':
                return self.public_signin(state)
        state = {'id': uuid.uuid4().hex, 'connection': connection_id, 'status': 'pending', 'events': [], 'prompt': None}
        self.signins[state['id']] = state
        async def login():
            try:
                async for event in self.events(connection_id, {'action': 'login'}, state):
                    if event['type'] == 'result':
                        state['status'] = 'connected'
                        state['prompt'] = None
                    elif event['type'] == 'prompt':
                        state['prompt'] = {'id': event['id'], **event['event']}
                    elif event['type'] == 'auth':
                        state['events'].append(event['event'])
            except asyncio.CancelledError:
                state['status'] = 'cancelled'
                raise
            except Exception as error:
                state['status'] = 'error'
                state['error'] = str(error)
        state['task'] = asyncio.create_task(login())
        return self.public_signin(state)

    @staticmethod
    def public_signin(state):
        return {k: v for k, v in state.items() if k not in ('process', 'task')}

    async def reply_login(self, login_id, prompt_id, value):
        state = self.signins[login_id]
        if state['status'] != 'pending' or not state['prompt'] or state['prompt']['id'] != prompt_id:
            raise ValueError('Sign-in step has expired')
        process = state['process']
        process.stdin.write((json.dumps({'id': prompt_id, 'value': value}) + '\n').encode())
        await process.stdin.drain()
        state['prompt'] = None


_service = None


def get_service():
    global _service
    if _service is None:
        _service = ModelService()
    return _service
