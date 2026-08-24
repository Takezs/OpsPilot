"""Prompt rendering for the restricted agent loop."""

import json

from opspilot.tools.types import ToolDefinition

_DECISION_CONTRACT = (
    "Decide one action and answer with a single JSON object of exactly one of "
    "these shapes:\n"
    '{"type": "answer", "answer": "..."}\n'
    '{"type": "clarify", "question": "..."}\n'
    '{"type": "tool_call", "tool": "<name>", "arguments": {...}}'
)


def render_tool_manifest(tools: tuple[ToolDefinition, ...]) -> str:
    manifest = [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema.model_json_schema(),
        }
        for tool in tools
    ]
    return json.dumps(manifest, ensure_ascii=False)


def render_system_prompt(tools: tuple[ToolDefinition, ...]) -> str:
    return (
        "You are a helpful assistant. Use only the tools listed below; every "
        "tool call must provide arguments matching its JSON schema. Do not "
        "reveal internal reasoning.\n\n"
        f"Tools:\n{render_tool_manifest(tools)}\n\n"
        f"{_DECISION_CONTRACT}"
    )
