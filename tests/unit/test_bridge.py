"""The MCP -> LangChain bridge: schema compilation, allow-lists, closure routing, error contract."""
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from backend.mcp_client.tools import (
    GROUP_TOOL_NAMES,
    PRIVATE_TOOL_NAMES,
    _args_model,
    _result_text,
    build_tools,
)


class DummyClient:
    """Records which tool each bridge coroutine actually routes to."""

    def __init__(self):
        self.routed: list[str] = []

    async def call_tool(self, name, args, **kwargs):
        self.routed.append(name)
        return SimpleNamespace(content=[SimpleNamespace(text=f"ran:{name}")], is_error=False)


# ---------------------------------------------------------------- _args_model
def _model(schema) -> type[BaseModel]:
    return _args_model("t", schema)


def test_flat_schema():
    m = _model({"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                "required": ["a"]})
    assert m.model_fields["a"].is_required()
    assert not m.model_fields["b"].is_required()
    assert m.model_fields["b"].default is None


def test_default_value_kept():
    m = _model({"type": "object", "properties": {"c": {"type": "string", "default": "other"}}})
    assert m.model_fields["c"].default == "other"


def test_anyof_null_becomes_optional():
    m = _model({"type": "object", "properties": {"d": {"anyOf": [{"type": "string"}, {"type": "null"}]}}})
    assert not m.model_fields["d"].is_required()
    m2 = _model({"type": "object", "required": ["e"],
                 "properties": {"e": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}})
    assert m2.model_fields["e"].is_required()
    assert m2.model_fields["e"].default is PydanticUndefined  # required AND nullable: no default


def test_array_of_primitives():
    m = _model({"type": "object", "required": ["xs"],
                "properties": {"xs": {"type": "array", "items": {"type": "string"}}}})
    assert m.model_fields["xs"].is_required()
    assert str(m.model_fields["xs"].annotation).startswith("list")


def test_array_of_objects_nested_required():
    m = _model({"type": "object", "required": ["items"],
                "properties": {"items": {"type": "array", "items": {
                    "type": "object",
                    "required": ["amount", "description"],
                    "properties": {
                        "amount": {"type": "number"},
                        "description": {"type": "string"},
                        "date": {"type": "string"},
                    },
                }}}})
    inner = m.model_fields["items"].annotation.__args__[0]
    assert {"amount", "description"} <= {n for n, f in inner.model_fields.items() if f.is_required()}
    assert not inner.model_fields["date"].is_required()


# ---------------------------------------------------------------- allow-lists
def test_allow_lists_shape():
    assert len(PRIVATE_TOOL_NAMES) == 18
    assert len(GROUP_TOOL_NAMES) == 6


# ---------------------------------------------------------------- build_tools routing
async def test_closure_routes_to_the_right_tool():
    dummy = DummyClient()
    specs = {n: SimpleNamespace(name=n, description="", args_model=_model({"type": "object", "properties": {}}))
             for n in sorted(PRIVATE_TOOL_NAMES)}
    _, by_name = build_tools(specs, dummy, chat_type="private")
    for name in ("respond_to_user", "add_expenses", "undo_last_expense", "list_friends"):
        out = await by_name[name].ainvoke({})
        assert out == f"ran:{name}"
        assert dummy.routed[-1] == name  # each tool calls ITSELF, not the last one built


async def test_group_binding_only_group_tools():
    dummy = DummyClient()
    specs = {n: SimpleNamespace(name=n, description="", args_model=_model({"type": "object", "properties": {}}))
             for n in PRIVATE_TOOL_NAMES | GROUP_TOOL_NAMES}
    tools, by_name = build_tools(specs, dummy, chat_type="group")
    assert {t.name for t in tools} == GROUP_TOOL_NAMES
    assert "add_expenses" not in by_name and "add_group_expense" in by_name


async def test_unknown_tools_are_skipped():
    dummy = DummyClient()
    specs = {"respond_to_user": SimpleNamespace(name="respond_to_user", description="",
                                                args_model=_model({"type": "object", "properties": {}})),
             "from_the_future": SimpleNamespace(name="from_the_future", description="",
                                                args_model=_model({"type": "object", "properties": {}}))}
    tools, by_name = build_tools(specs, dummy, chat_type="private")
    assert [t.name for t in tools] == ["respond_to_user"]


# ---------------------------------------------------------------- error contract
async def test_is_error_raises_for_the_agent_retry_loop():
    result = SimpleNamespace(content=[SimpleNamespace(text="boom")], is_error=True)
    with pytest.raises(RuntimeError, match="boom"):
        _result_text(result)


def test_text_joined_from_blocks():
    result = SimpleNamespace(content=[SimpleNamespace(text="a"), SimpleNamespace(text=""), SimpleNamespace(text="b")],
                             is_error=False)
    assert _result_text(result) == "a\nb"
