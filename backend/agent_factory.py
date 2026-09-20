# Copyright (C) 2025 AIDC-AI. Licensed under the MIT License.
from agents import Agent, ModelSettings, set_tracing_disabled
from .llm.model import CopilotModel

set_tracing_disabled(True)


def create_agent(**kwargs) -> Agent:
    config = kwargs.pop('config', None) or {}
    kwargs.pop('model', None)
    if config.get('max_tokens'):
        kwargs['model_settings'] = ModelSettings(max_tokens=config['max_tokens'])
    return Agent(model=CopilotModel(config), **kwargs)
