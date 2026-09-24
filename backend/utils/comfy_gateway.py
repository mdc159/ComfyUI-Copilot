"""
ComfyUI Gateway Utilities

This module provides Python implementations of ComfyUI API functions,
using HTTP requests to the ComfyUI server for consistency.
"""

import asyncio
import contextvars
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

import aiohttp


class ComfyUnreachable(Exception):
    """The target did not answer: connection refused or request timed out."""


class PromptTimeout(Exception):
    """wait_for_prompt gave up before the prompt reached history."""


class PromptLost(Exception):
    """The prompt left the queue without a history entry (deleted, wiped, or server restarted)."""


@dataclass(frozen=True)
class ComfyTarget:
    base_url: str                                             # 'http://127.0.0.1:8188'
    headers: Mapping[str, str] = field(default_factory=dict)  # auth for a proxied pod (Phase 7b)
    name: str = 'local'


_target: contextvars.ContextVar[Optional[ComfyTarget]] = contextvars.ContextVar('comfy_target', default=None)


def local_target() -> ComfyTarget:
    """The ComfyUI this process runs inside, or COPILOT_COMFY_URL when set."""
    if os.getenv('COPILOT_COMFY_URL'):
        return ComfyTarget(os.environ['COPILOT_COMFY_URL'].rstrip('/'))
    address, port = '127.0.0.1', 8188
    try:
        import server  # only importable inside the ComfyUI process
        instance = server.PromptServer.instance
        address = getattr(instance, 'address', None) or address
        port = getattr(instance, 'port', None) or port
    except Exception:
        pass
    if address in ('0.0.0.0', '::'):
        address = '127.0.0.1'
    elif ':' in address:
        address = f'[{address}]'
    return ComfyTarget(f'http://{address}:{port}')


def get_target() -> ComfyTarget:
    return _target.get() or local_target()


def set_target(target: Optional[ComfyTarget]) -> contextvars.Token:
    return _target.set(target)


class ComfyGateway:
    """HTTP client for one ComfyUI target (the current one unless given)."""

    def __init__(self, target: Optional[ComfyTarget] = None, base_url: Optional[str] = None):
        if target is None:
            target = ComfyTarget(base_url.rstrip('/'), name='custom') if base_url else get_target()
        self.target = target
        self.base_url = target.base_url
        logging.info(f"ComfyGateway initialized with base_url: {self.base_url}")

    async def _json(self, method: str, path: str, *, json_body=None, timeout: float = 30) -> Tuple[int, Any]:
        """(status, parsed body or {}); raises ComfyUnreachable when the target does not answer."""
        url = f"{self.base_url}{path}"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout),
                                             headers=dict(self.target.headers)) as session:
                async with session.request(method, url, json=json_body) as response:
                    text = await response.text()
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            raise ComfyUnreachable(f"{method} {url}: {e!r}") from e
        try:
            return response.status, json.loads(text) if text else {}
        except ValueError:
            return response.status, {"raw": text}   # aiohttp's plain-text 404 page and the like

    async def _history_entry(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        status, body = await self._json('GET', f"/api/history/{prompt_id}")
        return body.get(prompt_id) if status == 200 and isinstance(body, dict) else None

    async def run_prompt(self, json_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run a prompt - HTTP call to ComfyUI /api/prompt endpoint
        
        This method sends an HTTP POST request to the ComfyUI server's /api/prompt endpoint
        to ensure consistent behavior and avoid code duplication.
        
        Args:
            json_data: The prompt/workflow data in the same format as HTTP API
            
        Returns:
            Dict containing the validation result, similar to HTTP API response
        """
        try:
            status, body = await self._json('POST', '/api/prompt', json_body=json_data)
            return {"success": status == 200, **body}
        except ComfyUnreachable as e:
            logging.error(f"Connection error in run_prompt: {e}")
            return {
                "success": False,
                "error": {
                    "type": "connection_error",
                    "message": f"Failed to connect to ComfyUI server at {self.base_url}",
                    "details": str(e)
                },
                "node_errors": {}
            }
        except Exception as e:
            logging.error(f"Error in run_prompt: {e}")
            return {
                "success": False,
                "error": {
                    "type": "internal_error",
                    "message": f"Internal error: {str(e)}",
                    "details": str(e)
                },
                "node_errors": {}
            }

    async def get_object_info(self, node_class: Optional[str] = None) -> Dict[str, Any]:
        """
        Get ComfyUI node definitions - HTTP call to ComfyUI /api/object_info endpoint
        
        Args:
            node_class: Optional specific node class to get info for
            
        Returns:
            Dict containing node definitions and their parameters
        """
        try:
            path = f"/api/object_info/{node_class}" if node_class else "/api/object_info"
            status, body = await self._json('GET', path)
            if status != 200:
                logging.error(f"Failed to get object info: HTTP {status}")
                return {}
            return body
        except Exception as e:
            logging.error(f"Error getting object info: {e}")
            return {}

    async def get_installed_nodes(self) -> List[str]:
        """
        Get list of installed node types - HTTP call to ComfyUI /api/object_info endpoint
        
        Returns:
            List of installed node type names
        """
        try:
            object_info = await self.get_object_info()
            return list(object_info.keys())
        except Exception as e:
            logging.error(f"Error getting installed nodes: {e}")
            return []

    async def manage_queue(self, clear: bool = False, delete: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Clear the prompt queue or delete specific queue items - HTTP call to ComfyUI /api/queue endpoint
        
        Args:
            clear: If True, clears the entire queue
            delete: List of prompt IDs to delete from the queue
            
        Returns:
            Dict with the response from the queue management operation
        """
        try:
            json_data = {}
            if clear:
                json_data["clear"] = True
            if delete:
                json_data["delete"] = delete
            status, _ = await self._json('POST', '/api/queue', json_body=json_data)
            if status != 200:
                logging.error(f"Failed to manage queue: HTTP {status}")
                return {"error": f"HTTP {status}"}
            return {"success": True}
        except ComfyUnreachable as e:
            logging.error(f"Connection error in manage_queue: {e}")
            return {"error": f"Connection error: {str(e)}"}
        except Exception as e:
            logging.error(f"Error managing queue: {e}")
            return {"error": f"Failed to manage queue: {str(e)}"}

    async def interrupt_processing(self) -> Dict[str, Any]:
        """
        Interrupt the current processing/generation - HTTP call to ComfyUI /api/interrupt endpoint
        
        Returns:
            Dict with the response from the interrupt operation
        """
        try:
            status, _ = await self._json('POST', '/api/interrupt')
            if status != 200:
                logging.error(f"Failed to interrupt processing: HTTP {status}")
                return {"error": f"HTTP {status}"}
            return {"success": True}
        except ComfyUnreachable as e:
            logging.error(f"Connection error in interrupt_processing: {e}")
            return {"error": f"Connection error: {str(e)}"}
        except Exception as e:
            logging.error(f"Error interrupting processing: {e}")
            return {"error": f"Failed to interrupt processing: {str(e)}"}

    async def get_history(self, prompt_id: str) -> Dict[str, Any]:
        """
        Get execution history for a specific prompt - HTTP call to ComfyUI /api/history/{prompt_id} endpoint
        
        Args:
            prompt_id: The ID of the prompt to get history for
            
        Returns:
            Dict containing the execution history and results
        """
        try:
            status, body = await self._json('GET', f"/api/history/{prompt_id}")
            if status != 200:
                logging.error(f"Failed to get history for prompt {prompt_id}: HTTP {status}")
                return {"error": f"HTTP {status}"}
            return body
        except ComfyUnreachable as e:
            logging.error(f"Connection error in get_history: {e}")
            return {"error": f"Connection error: {str(e)}"}
        except Exception as e:
            logging.error(f"Error fetching history for prompt {prompt_id}: {e}")
            return {"error": f"Failed to get history: {str(e)}"}

    async def get_queue_status(self) -> Dict[str, Any]:
        """
        Get current queue status - HTTP call to ComfyUI /api/queue endpoint
        
        Returns:
            Dict containing current queue information
        """
        try:
            status, body = await self._json('GET', '/api/queue')
            if status != 200:
                logging.error(f"Failed to get queue status: HTTP {status}")
                return {"error": f"HTTP {status}"}
            return body
        except ComfyUnreachable as e:
            logging.error(f"Connection error in get_queue_status: {e}")
            return {"error": f"Connection error: {str(e)}"}
        except Exception as e:
            logging.error(f"Error getting queue status: {e}")
            return {"error": f"Failed to get queue status: {str(e)}"}

    async def validate_prompt(self, prompt: Dict[str, Any]) -> Dict[str, Any]:
        """Structural check via the Copilot-owned route; {'supported': False} when the target lacks it."""
        status, body = await self._json('POST', '/api/copilot/validate', json_body={"prompt": prompt})
        if status == 404:
            return {"supported": False}
        if status != 200:
            return {"supported": True, "valid": False, "node_errors": {}, "outputs": [],
                    "error": {"type": "http_error", "message": f"HTTP {status}", "details": body}}
        return {"supported": True, **body}

    async def get_system_stats(self) -> Dict[str, Any]:
        status, body = await self._json('GET', '/api/system_stats')
        return body if status == 200 else {"error": f"HTTP {status}"}

    async def cancel_prompt(self, prompt_id: str) -> None:
        # Pending prompts leave via the queue delete, running ones via the targeted interrupt;
        # each is a no-op for the other state, so both are sent.
        await self._json('POST', '/api/queue', json_body={"delete": [prompt_id]})
        await self._json('POST', '/api/interrupt', json_body={"prompt_id": prompt_id})

    async def wait_for_prompt(self, prompt_id: str, timeout: float, poll: float = 1.0) -> Dict[str, Any]:
        """Poll until the prompt has a history entry; PromptLost if it leaves the queue without one."""
        deadline = time.monotonic() + timeout
        while True:
            entry = await self._history_entry(prompt_id)
            if entry is not None:
                return entry
            _, queue = await self._json('GET', '/api/queue')
            queued = {item[1] for key in ("queue_running", "queue_pending")
                      for item in queue.get(key, []) if len(item) > 1}
            if prompt_id not in queued:
                # task_done pops the queue and writes history under one mutex, but we observe
                # that through two requests, so the entry may have landed since the first look.
                entry = await self._history_entry(prompt_id)
                if entry is not None:
                    return entry
                raise PromptLost(prompt_id)
            if time.monotonic() > deadline:
                raise PromptTimeout(prompt_id)
            await asyncio.sleep(poll)


# Convenience functions for backward compatibility and easy importing
async def run_prompt(json_data: Dict[str, Any], base_url: Optional[str] = None) -> Dict[str, Any]:
    """
    Standalone function to run a prompt - HTTP call to ComfyUI /api/prompt endpoint
    
    Args:
        json_data: The prompt/workflow data to execute
        base_url: Optional base URL for ComfyUI server
        
    Returns:
        Dict containing the API response
    """
    gateway = ComfyGateway(base_url=base_url)
    return await gateway.run_prompt(json_data)


async def get_object_info(base_url: Optional[str] = None) -> Dict[str, Any]:
    """Standalone function to get object info - HTTP call to ComfyUI /api/object_info endpoint"""
    gateway = ComfyGateway(base_url=base_url)
    return await gateway.get_object_info()

async def get_object_info_by_class(node_class: str, base_url: Optional[str] = None) -> Dict[str, Any]:
    """Standalone function to get object info for specific node class - HTTP call to ComfyUI /api/object_info/{node_class} endpoint"""
    gateway = ComfyGateway(base_url=base_url)
    return await gateway.get_object_info(node_class)


async def get_installed_nodes(base_url: Optional[str] = None) -> List[str]:
    """Standalone function to get installed nodes - HTTP call to ComfyUI /api/object_info endpoint"""
    gateway = ComfyGateway(base_url=base_url)
    return await gateway.get_installed_nodes()

async def manage_queue(clear: bool = False, delete: Optional[List[str]] = None, base_url: Optional[str] = None) -> Dict[str, Any]:
    """Standalone function to manage queue - HTTP call to ComfyUI /api/queue endpoint"""
    gateway = ComfyGateway(base_url=base_url)
    return await gateway.manage_queue(clear, delete)

async def interrupt_processing(base_url: Optional[str] = None) -> Dict[str, Any]:
    """Standalone function to interrupt processing - HTTP call to ComfyUI /api/interrupt endpoint"""
    gateway = ComfyGateway(base_url=base_url)
    return await gateway.interrupt_processing()

async def get_history(prompt_id: str, base_url: Optional[str] = None) -> Dict[str, Any]:
    """Standalone function to get history - HTTP call to ComfyUI /api/history/{prompt_id} endpoint"""
    gateway = ComfyGateway(base_url=base_url)
    return await gateway.get_history(prompt_id)

async def get_queue_status(base_url: Optional[str] = None) -> Dict[str, Any]:
    """Standalone function to get queue status - HTTP call to ComfyUI /api/queue endpoint"""
    gateway = ComfyGateway(base_url=base_url)
    return await gateway.get_queue_status()
