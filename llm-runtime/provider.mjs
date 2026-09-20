// Provider transports and OAuth only. Copilot's Python Agents SDK owns tool execution.
import { createInterface } from 'node:readline';
import { InMemoryCredentialStore, createProvider } from '@earendil-works/pi-ai';
import { builtinModels } from '@earendil-works/pi-ai/providers/all';
import { openAICompletionsApi } from '@earendil-works/pi-ai/api/openai-completions.lazy';

const lines = createInterface({ input: process.stdin });
const pending = new Map();
let started = false;
const send = value => process.stdout.write(JSON.stringify(value) + '\n');
const usage = () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } });

function content(parts) {
  if (typeof parts === 'string') return [{ type: 'text', text: parts }];
  return (parts || []).map(p => {
    if (p.type === 'text') return { type: 'text', text: p.text };
    if (p.type === 'image_url') {
      const match = /^data:([^;]+);base64,(.+)$/s.exec(p.image_url.url);
      if (!match) throw new Error('Image input must be an inline data URL');
      return { type: 'image', mimeType: match[1], data: match[2] };
    }
    throw new Error(`Unsupported model input content: ${p.type}`);
  });
}

export function contextFromChat(messages, model, tools = [], schema) {
  const result = { systemPrompt: '', messages: [], tools: tools.map(t => ({
    name: t.function.name, description: t.function.description || '', parameters: t.function.parameters,
  })) };
  const names = new Map();
  for (const m of messages) {
    if (m.role === 'system' || m.role === 'developer') {
      result.systemPrompt += content(m.content).map(p => p.text || '').join('\n') + '\n';
    } else if (m.role === 'user') {
      result.messages.push({ role: 'user', content: content(m.content), timestamp: Date.now() });
    } else if (m.role === 'assistant') {
      const blocks = content(m.content);
      for (const t of m.tool_calls || []) {
        names.set(t.id, t.function.name);
        blocks.push({ type: 'toolCall', id: t.id, name: t.function.name, arguments: JSON.parse(t.function.arguments || '{}') });
      }
      result.messages.push({ role: 'assistant', content: blocks, api: model.api, provider: model.provider,
        model: model.id, usage: usage(), stopReason: m.tool_calls?.length ? 'toolUse' : 'stop', timestamp: Date.now() });
    } else if (m.role === 'tool') {
      result.messages.push({ role: 'toolResult', toolCallId: m.tool_call_id,
        toolName: names.get(m.tool_call_id) || m.name || 'tool', content: content(m.content), isError: false, timestamp: Date.now() });
    } else throw new Error(`Unsupported message role: ${m.role}`);
  }
  if (schema) result.systemPrompt += '\nReturn only JSON matching this schema: ' + JSON.stringify(schema);
  return result;
}

async function main(request) {
  // Latchkey injects the configured key name into this process, including custom aliases.
  if (!request.credential && request.envVar && process.env[request.envVar]) {
    request.credential = { type: 'api_key', key: process.env[request.envVar] };
  }
  const store = new InMemoryCredentialStore();
  if (request.credential) await store.modify(request.provider, async () => request.credential);
  // Every refresh is delivered to the owning Python credential store before any model output.
  const modify = store.modify.bind(store);
  store.modify = async (...args) => {
    const next = await modify(...args);
    if (next) send({ type: 'credential', value: next });
    return next;
  };
  const models = builtinModels({ credentials: store });
  if (request.action === 'providers') {
    send({ type: 'result', value: models.getProviders().map(p => ({ id: p.id, name: p.name,
      oauth: Boolean(p.auth.oauth), apiKey: Boolean(p.auth.apiKey) })) });
    return;
  }
  if (request.action === 'login') {
    await models.login(request.provider, 'oauth', {
      signal: AbortSignal.timeout(15 * 60 * 1000),
      notify: event => send({ type: 'auth', event }),
      prompt: p => new Promise((resolve, reject) => {
        const id = crypto.randomUUID();
        pending.set(id, resolve);
        p.signal?.addEventListener('abort', () => { pending.delete(id); reject(new Error('Sign-in step cancelled')); }, { once: true });
        const { signal, ...event } = p;
        send({ type: 'prompt', id, event });
      }),
    });
    send({ type: 'result', value: { connected: true } });
    return;
  }
  if (request.action === 'catalog') {
    if (request.baseUrl) {
      const key = request.credential?.key || process.env[request.envVar];
      const response = await fetch(request.baseUrl + '/models', {
        headers: key ? { Authorization: 'Bearer ' + key } : {}, signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) throw new Error(`Model catalog returned HTTP ${response.status}`);
      const body = await response.json();
      send({ type: 'result', value: body.data.map(m => ({ id: m.id, name: m.id, image: false })) });
      return;
    }
    send({ type: 'result', value: models.getModels(request.provider).map(m => ({ id: m.id,
      name: m.name, image: m.input.includes('image'), reasoning: m.reasoning, context: m.contextWindow })) });
    return;
  }
  let model;
  if (request.baseUrl) {
    model = { id: request.model, name: request.model, provider: 'copilot-custom', api: 'openai-completions',
      baseUrl: request.baseUrl, reasoning: false, input: ['text', 'image'], contextWindow: 32768, maxTokens: 8192,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
    models.setProvider(createProvider({ id: model.provider, name: 'Copilot endpoint', baseUrl: request.baseUrl,
      auth: { apiKey: { name: 'Copilot credential', resolve: async () => ({
        auth: { apiKey: request.credential?.key || process.env[request.envVar] || 'local-no-key' },
        source: 'copilot' }) } }, models: [model], api: openAICompletionsApi() }));
  } else {
    model = models.getModel(request.provider, request.model);
    if (!model) throw new Error('Model is not in this provider catalog. Refresh models and select again.');
  }
  const context = contextFromChat(request.messages, model, request.tools, request.schema);
  const stream = models.streamSimple(model, context, {
    maxTokens: request.maxTokens || 8192,
    ...(request.temperature == null ? {} : { temperature: request.temperature }),
    ...(request.toolChoice == null ? {} : { toolChoice: request.toolChoice }),
    ...(request.reasoning ? { reasoning: request.reasoning } : {}),
    signal: AbortSignal.timeout(5 * 60 * 1000),
  });
  for await (const event of stream) {
    if (event.type === 'text_delta') send({ type: 'delta', text: event.delta });
    if (event.type === 'error') throw new Error(event.error?.errorMessage || 'Provider request failed');
  }
  const response = await stream.result();
  if (response.stopReason === 'error' || response.stopReason === 'aborted') throw new Error(response.errorMessage || 'Provider request failed');
  send({ type: 'result', value: { text: response.content.filter(p => p.type === 'text').map(p => p.text).join(''),
    tool_calls: response.content.filter(p => p.type === 'toolCall').map(p => ({ id: p.id, type: 'function',
      function: { name: p.name, arguments: JSON.stringify(p.arguments) } })), usage: response.usage,
    stopReason: response.stopReason } });
}

lines.on('line', line => {
  const request = JSON.parse(line);
  if (started) { pending.get(request.id)?.(request.value); pending.delete(request.id); return; }
  started = true;
  main(request).catch(error => {
    let message = String(error.message || 'Provider operation failed');
    for (const value of [...Object.values(request.credential || {}), process.env[request.envVar]]) {
      if (typeof value === 'string' && value.length > 8) message = message.replaceAll(value, '[redacted]');
    }
    send({ type: 'error', message });
  }).finally(() => process.exit(0));
});
