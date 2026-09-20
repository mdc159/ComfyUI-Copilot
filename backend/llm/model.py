"""Provider implementation for Copilot's existing OpenAI Agents SDK."""
import time
import uuid

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.models.chatcmpl_converter import Converter
from agents.usage import Usage
from openai.types.chat import ChatCompletionMessage
from openai.types.responses import Response, ResponseCompletedEvent, ResponseCreatedEvent, ResponseTextDeltaEvent

from .service import get_service


class CopilotModel(Model):
    def __init__(self, config=None, service=None):
        self.service = service or get_service()
        self.selection = self.service.selection(config)

    def payload(self, system_instructions, input, model_settings, tools, output_schema, handoffs):
        if model_settings.tool_choice not in (None, 'auto', 'none'):
            raise ValueError('This model service supports automatic tool selection or disabled tools')
        messages = Converter.items_to_messages(input)
        if system_instructions:
            messages.insert(0, {'role': 'system', 'content': system_instructions})
        model_tools = [Converter.tool_to_openai(tool) for tool in tools]
        model_tools += [Converter.convert_handoff_tool(handoff) for handoff in handoffs]
        return {'action': 'complete', 'model': self.selection['model'], 'messages': messages,
                'tools': model_tools, 'maxTokens': model_settings.max_tokens,
                'temperature': model_settings.temperature,
                'toolChoice': model_settings.tool_choice,
                'schema': output_schema.json_schema() if output_schema else None}

    @staticmethod
    def output(result):
        return Converter.message_to_output_items(ChatCompletionMessage(
            role='assistant', content=result['text'] or None, tool_calls=result.get('tool_calls') or None))

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema,
                           handoffs, tracing, **kwargs):
        result = await self.service.call(self.selection['connection'], self.payload(
            system_instructions, input, model_settings, tools, output_schema, handoffs))
        usage = result.get('usage', {})
        return ModelResponse(output=self.output(result), usage=Usage(requests=1,
            input_tokens=usage.get('input', 0) + usage.get('cacheRead', 0) + usage.get('cacheWrite', 0),
            output_tokens=usage.get('output', 0), total_tokens=usage.get('totalTokens', 0)), response_id=None)

    async def stream_response(self, system_instructions, input, model_settings, tools, output_schema,
                              handoffs, tracing, **kwargs):
        response_id = 'resp_' + uuid.uuid4().hex
        seq = 0
        def response(output, status, usage=None):
            return Response(id=response_id, created_at=time.time(), model=self.selection['model'],
                object='response', output=output, status=status, parallel_tool_calls=True,
                tool_choice='auto', tools=[], usage=usage)
        yield ResponseCreatedEvent(type='response.created', response=response([], 'in_progress'), sequence_number=seq)
        async for event in self.service.events(self.selection['connection'], self.payload(
                system_instructions, input, model_settings, tools, output_schema, handoffs)):
            seq += 1
            if event['type'] == 'delta':
                yield ResponseTextDeltaEvent(type='response.output_text.delta', delta=event['text'],
                    item_id=response_id + '_message', output_index=0, content_index=0, sequence_number=seq, logprobs=[])
            elif event['type'] == 'result':
                result = event['value']
                usage = result.get('usage', {})
                yield ResponseCompletedEvent(type='response.completed', sequence_number=seq,
                    response=response(self.output(result), 'completed', {
                        'input_tokens': usage.get('input', 0) + usage.get('cacheRead', 0) + usage.get('cacheWrite', 0),
                        'output_tokens': usage.get('output', 0), 'total_tokens': usage.get('totalTokens', 0),
                        'input_tokens_details': {'cached_tokens': usage.get('cacheRead', 0),
                            'cache_write_tokens': usage.get('cacheWrite', 0)},
                        'output_tokens_details': {'reasoning_tokens': usage.get('reasoning', 0)},
                    }))


def structured_completion(config, messages, response_type, **options):
    result = get_service().complete_sync(config, messages, schema=response_type.model_json_schema(), **options)
    text = result['text'].strip()
    if text.startswith('```'):
        text = '\n'.join(text.splitlines()[1:-1])
    return response_type.model_validate_json(text)
