"""System stats of the current ComfyUI target, trimmed to what an agent needs."""
from typing import Any, Dict, Optional

from agents.tool import function_tool

from ..utils.comfy_gateway import ComfyGateway
from ._common import tool_json

GB = 1024 ** 3


def _gb(value: Any) -> Optional[float]:
    return round(value / GB, 2) if isinstance(value, (int, float)) else None


async def system_stats(gateway: Optional[ComfyGateway] = None) -> Dict[str, Any]:
    """{"comfyui_version", "pytorch_version", "argv", "devices": [{"name", "vram_total_gb", "vram_free_gb"}]}.

    `argv` shows whether flags like --lowvram are already on. Raises ComfyUnreachable.
    """
    raw = await (gateway or ComfyGateway()).get_system_stats()
    if "system" not in raw:
        return {"error": raw.get("error", "unexpected /api/system_stats reply")}
    system = raw.get("system") or {}
    return {
        "comfyui_version": system.get("comfyui_version"),
        "pytorch_version": system.get("pytorch_version"),
        "argv": system.get("argv", []),
        "devices": [{"name": device.get("name"),
                     "vram_total_gb": _gb(device.get("vram_total")),
                     "vram_free_gb": _gb(device.get("vram_free"))}
                    for device in raw.get("devices") or []],
    }


@function_tool
async def get_system_stats() -> str:
    """ComfyUI version, PyTorch version, launch arguments and per-GPU VRAM (total/free, GB) of the target."""
    try:
        return tool_json(await system_stats())
    except Exception as e:
        return tool_json({"error": f"Failed to get system stats: {e}"})
