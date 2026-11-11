"""
Turning MCP tool definitions into what an LLM is shown.

The LLM must not see (or invent) the protocol arguments - the bot supplies them itself - and many model APIs dislike
`$ref` / `anyOf: [..., null]`, so the JSON schema is flattened into the plain shape every provider accepts.
"""
import copy
from typing import Any

HIDDEN_ARGS = ("answers", "idempotency_key", "member_count")


def _inline(node: Any, defs: dict) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            target = defs.get(node["$ref"].split("/")[-1], {})
            return _inline({**copy.deepcopy(target), **{k: v for k, v in node.items() if k != "$ref"}}, defs)
        out = {k: _inline(v, defs) for k, v in node.items() if k not in ("$defs", "title", "additionalProperties")}
        any_of = out.get("anyOf")
        if isinstance(any_of, list):
            non_null = [x for x in any_of if x.get("type") != "null"]
            if len(non_null) == 1:  # Optional[X]  ->  X  (optionality is expressed by not being in `required`)
                merged = {**non_null[0], **{k: v for k, v in out.items() if k != "anyOf"}}
                return merged
        if out.get("default", 0) is None:
            out.pop("default")
        return out
    if isinstance(node, list):
        return [_inline(x, defs) for x in node]
    return node


def llm_parameters(input_schema: dict) -> dict:
    """The tool's JSON schema as an LLM should see it: flat, no protocol arguments."""
    schema = _inline(copy.deepcopy(input_schema), input_schema.get("$defs", {}))
    props = {k: v for k, v in schema.get("properties", {}).items() if k not in HIDDEN_ARGS}
    required = [r for r in schema.get("required", []) if r not in HIDDEN_ARGS]
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def llm_tool(name: str, description: str, input_schema: dict) -> dict:
    """One tool in the plain `{name, description, parameters}` form that `bind_tools` accepts."""
    doc = (description or name).split("\n\nProtocol arguments")[0].strip()
    return {"name": name, "description": doc, "parameters": llm_parameters(input_schema)}
