'''
Author: ai-business-hql ai.bussiness.hql@gmail.com
Date: 2025-11-19
LastEditors: ai-business-hql ai.bussiness.hql@gmail.com
LastEditTime: 2025-11-19
FilePath: /ComfyUI-Copilot/backend/service/summary_agent.py
Description: Summary agent for compressing conversation history
'''

import json
from typing import List, Dict, Any
from pydantic import BaseModel
from ..llm.model import structured_completion

from ..utils.key_utils import workflow_config_adapt
from ..utils.request_context import get_config
from ..utils.logger import log


class SummaryResponse(BaseModel):
    """
    对话历史摘要响应，必须是简洁的英文文本
    """
    summary: str


def generate_summary(messages: List[Dict[str, Any]], previous_summary: str = None) -> str:
    """
    生成对话历史摘要
    
    Args:
        messages: 需要摘要的消息列表，格式为 [{"role": "user/assistant", "content": "..."}]
        previous_summary: 之前的摘要（如果有），新摘要会整合历史信息
        
    Returns:
        生成的摘要文本，不超过 200 words
    """
    try:
        # 构建消息内容文本
        messages_text = "\n\n".join([
            f"**{msg['role'].upper()}**: {msg['content']}" 
            for msg in messages
            if isinstance(msg.get('content'), str)  # 只处理文本内容
        ])
        
        # 构建用户提示
        if previous_summary:
            user_prompt = f"""Please generate a concise summary of the following conversation history, integrating it with the previous summary.

## Previous Summary
{previous_summary}

## New Conversation Messages
{messages_text}

## Requirements
- Maximum 200 words
- Focus on key topics, decisions, and important context
- Use clear, objective language
- Integrate previous summary seamlessly with new information
- Avoid redundancy
"""
        else:
            user_prompt = f"""Please generate a concise summary of the following conversation history.

## Conversation Messages
{messages_text}

## Requirements
- Maximum 200 words
- Focus on key topics, decisions, and important context
- Use clear, objective language
"""

        # 系统提示词
        system_prompt = """You are an expert conversation summarizer. Your task is to create concise, informative summaries of conversation histories that preserve the most important information while reducing token usage.

Focus on:
- Main topics discussed
- Key decisions or conclusions
- Important technical details or requirements
- User's goals and preferences
- Context that would be needed for future interactions

Keep summaries factual, objective, and efficient."""

        # 获取配置
        config = get_config() or {}

        result = structured_completion(config, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], SummaryResponse)
        summary_text = result.summary

        log.info(f"Generated summary: {summary_text[:100]}..." if summary_text else "Generated empty summary")

        if summary_text:
            return summary_text
        else:
            log.warning("Summary generation returned empty result")
            return ""

    except Exception as e:
        log.error(f"Failed to generate summary: {str(e)}")
        # 返回一个简单的回退摘要
        fallback = f"Conversation with {len(messages)} messages"
        if previous_summary:
            fallback = f"{previous_summary} | {fallback}"
        return fallback


def test_summary_agent():
    """测试摘要生成功能"""
    test_messages = [
        {"role": "user", "content": "I want to create an image generation workflow using Stable Diffusion"},
        {"role": "assistant", "content": "I can help you with that. Let me search for relevant workflows..."},
        {"role": "user", "content": "I need it to support LoRA models"},
        {"role": "assistant", "content": "I've found some workflows with LoRA support. Here are the options..."},
    ]
    
    summary = generate_summary(test_messages)
    print("Generated Summary:", summary)
    
    # 测试增量摘要
    new_messages = [
        {"role": "user", "content": "Can you also add upscaling?"},
        {"role": "assistant", "content": "Sure, I'll modify the workflow to include upscaling nodes..."},
    ]
    
    updated_summary = generate_summary(new_messages, previous_summary=summary)
    print("Updated Summary:", updated_summary)


if __name__ == "__main__":
    test_summary_agent()

