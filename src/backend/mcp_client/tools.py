"""Bridge: MCP tools (list_tools / call_tool) <-> LangChain StructuredTools.

Discovery (which tools exist + their schemas) is done once per deploy.
Binding (making LangChain tools bound to ONE run's client) is per run,
because each run gets its own mcp_session() with its own token.
"""
from backend.mcp_client.client import mcp_session   # no cycle: client.py imports nothing from tools.py
from dataclasses import dataclass
from typing import Any

from fastmcp import Client
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from backend.mcp_client.client import mcp_session  # no cycle: client.py imports nothing from tools.py

PRIVATE_TOOL_NAMES = frozenset({
    "respond_to_user",
    "add_expenses", "undo_last_expense",
    "set_budget", "get_budgets", "remove_budget",
    "get_expenses", "spending_summary", "monthly_trend", "daily_breakdown",
    "add_friend", "remove_friend", "list_friends",
    "split_expense", "split_bills", "my_dues", "cancel_split",
    "create_group",
})
GROUP_TOOL_NAMES = frozenset({
    "respond_to_user",
    "add_group_expense", "split_bills", "group_summary", "settle_group", "cancel_split",
})


def _args_model(tool_name: str, schema: dict) -> type[BaseModel]:
    """Compile fastmcp's JSON Schema into a pydantic model so the LLM gets structured args."""
    type_map = {"string": str, "integer": int, "number": float, "boolean": bool}

    def field(name: str, spec: dict, required_set: set[str], prefix: str):
        # anyOf [X, null] -> X, nullable
        nullable = False
        if "anyOf" in spec:
            options = [s for s in spec["anyOf"] if s.get("type") != "null"]
            nullable = len(options) < len(spec["anyOf"])
            if not options:
                return str | None, Field(default=None, description=spec.get("description"))
            spec = {**{k: v for k, v in spec.items() if k != "anyOf"}, **options[0]}

        t = spec.get("type")
        if t == "array":
            item = spec.get("items", {})
            if item.get("type") == "object":
                py_type = list[_object(item, f"{prefix}{name}")]
            else:
                py_type = list[type_map.get(item.get("type"), Any)]
        elif t == "object":
            py_type = dict
        elif t in type_map:
            py_type = type_map[t]
        else:
            raise ValueError(f"{tool_name}.{name}: unsupported schema type {spec}")

        if name in required_set:
            ann = py_type | None if nullable else py_type
            default = spec.get("default", ...)
        else:
            ann = py_type | None
            default = spec.get("default", None)
        return ann, Field(default=default, description=spec.get("description"))

    def _object(obj: dict, prefix: str) -> type[BaseModel]:
        nested_required = set(obj.get("required", []))
        return create_model(
            prefix,
            **{n: field(n, s, nested_required, prefix)
               for n, s in obj.get("properties", {}).items()},  # type: ignore[arg-type]
        )

    fields = {name: field(name, spec, set(schema.get("required", [])), tool_name)
              for name, spec in schema.get("properties", {}).items()}
    return create_model(f"{tool_name}_args", **fields)


# ---------------------------------------------------------------- call + result
def _result_text(result) -> str:
    blocks = [getattr(b, "text", "") for b in result.content]
    text = "\n".join(b for b in blocks if b)
    if result.is_error:
        raise RuntimeError(text or "tool call failed")
    return text


@dataclass
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]


async def discover(client: Client) -> dict[str, ToolSpec]:
    """One-time (per deploy) discovery: fetch tool schemas and compile them."""
    out = {}
    for t in await client.list_tools():
        schema = getattr(t, "input_schema", None) or t.inputSchema
        out[t.name] = ToolSpec(t.name, t.description or "", _args_model(t.name, schema))
    return out



# ---------------------------------------------------------------- per-run binding
def build_tools(
    specs: dict[str, ToolSpec], client: Client | None, *, chat_type: str
) -> tuple[list[StructuredTool], dict[str, StructuredTool]]:
    """
    Bind this run's client to the tools allowed in this chat context.
    Returns (bindable_list, by_name) — the graph.py replacements for
    PRIVATE_TOOLS/GROUP_TOOLS/TOOLS_BY_NAME, but per-run.
    """
    allowed = (GROUP_TOOL_NAMES if chat_type == "group" else PRIVATE_TOOL_NAMES) & set(specs)
    by_name: dict[str, StructuredTool] = {}
    for name in sorted(allowed):
        spec = specs[name]

        async def call(_spec=spec, _client=client, **kwargs) -> str:
            return _result_text(await _client.call_tool(_spec.name, kwargs, raise_on_error=False))

        tool = StructuredTool(name=spec.name, description=spec.description,
                              args_schema=spec.args_model, coroutine=call)
        by_name[name] = tool
    return list(by_name.values()), by_name

# ---------------------------------------------------------------- discovery cache (per deploy)
_specs_cache: dict[str, ToolSpec] | None = None


async def get_specs(client: Client) -> dict[str, ToolSpec]:
    """Cached discovery: list_tools runs once per deploy, everything after that reuses the schemas."""
    global _specs_cache
    if _specs_cache is None:
        _specs_cache = await discover(client)
    return _specs_cache


async def init_discovery() -> int:
    """Warm the cache at bot startup. Optional — the first real message discovers lazily anyway."""
    async with mcp_session(user_id=0, chat_type="private") as client:
        return len(await get_specs(client))
