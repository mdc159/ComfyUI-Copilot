"""Local tool implementations, one module per capability.

Each module exposes a plain async/sync function (testable, reusable by the MCP server)
and a thin ``@function_tool`` wrapper that reads session context and returns a JSON
string. Nothing in this package imports ``server``, ``nodes``, ``execution`` or
``folder_paths`` at module scope, so it loads outside the ComfyUI process.
"""
