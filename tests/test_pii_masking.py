"""Tests for reversible PII masking across chat agent, statement parser, and finance agent."""

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel

from app.agent.graph import build_graph
from app.agent.pii_masker import PIIMasker, build_chat_masker
from app.agent.statement_parser import parse_pages
from app.mcp_client.types import Identity, ToolResult
from finance_agent.claims import review
from finance_agent.manager import chat as mgr_chat
from finance_agent.policy import parser as policy_parser
from finance_agent.policy import qa as policy_qa
from finance_agent.policy.models import Item, Rule


class CapturingChatLLM(BaseChatModel):
    """Records exact messages sent to the LLM and plays back scripted replies."""

    script: list[AIMessage] = []
    recorded_messages: list[list] = []
    idx: int = 0

    @property
    def _llm_type(self) -> str:
        return "capturing"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.recorded_messages.append(list(messages))
        msg = self.script[self.idx]
        self.idx += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


class CapturingStructuredLLM:
    """Fake LLM supporting with_structured_output for statement_parser tests."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.recorded_messages: list[list] = []

    def with_structured_output(self, schema: type[BaseModel], **kwargs):
        outer = self

        class _Runnable:
            async def ainvoke(self, messages):
                outer.recorded_messages.append(list(messages))
                return schema(transactions=outer.rows)

        return _Runnable()


class FakeGateway:
    """Minimal in-memory gateway recording tool calls without needing Postgres/MCP."""

    def __init__(self, context_data: dict):
        self.context_data = context_data
        self.calls: list[tuple[str, dict | None]] = []

    async def call(
        self,
        ident: Identity,
        tool: str,
        args: dict | None = None,
        **kwargs,
    ) -> ToolResult:
        self.calls.append((tool, args))
        if tool == "agent_context":
            return ToolResult(status="ok", text="", data=self.context_data)
        return ToolResult(status="ok", text="Done.", data={"expense_ids": [1]})


class CapturingFinanceLLM:
    """Records raw (system, user) strings received by the Finance Agent LLM."""

    def __init__(self, *responses: dict):
        self.queue = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def json(self, system: str, user: str, *, timeout: int | None = None) -> dict:
        self.calls.append((system, user))
        return self.queue.pop(0)


# ------------------------------------------------------------------ PIIMasker unit tests
def test_masker_masks_names_and_structured_pii_without_touching_amounts_or_dates():
    masker = build_chat_masker(
        speaker="Kiran Rao",
        username="kiran_r",
        telegram_id=112233445,
        group="Goa Trip",
        friends=["Vikram", "Neha"],
        members=["Kiran Rao", "Ananya"],
    )
    raw = (
        "Hi Vikram, Neha and Ananya, on 2026-11-25 I spent 1500.50 in Goa Trip. "
        "Email kiran@example.com, UPI vikram@okicici, phone +91 98765 43210, "
        "A/c 123456789012, Card 4532-1234-5678-9012, IFSC HDFC0001234, PAN ABCDE1234F."
    )
    masked = masker.mask_text(raw)

    for sensitive in (
        "Vikram",
        "Neha",
        "Ananya",
        "Goa Trip",
        "kiran@example.com",
        "vikram@okicici",
        "+91 98765 43210",
        "123456789012",
        "4532-1234-5678-9012",
        "HDFC0001234",
        "ABCDE1234F",
    ):
        assert sensitive not in masked

    assert "1500.50" in masked
    assert "2026-11-25" in masked
    assert "FRIEND_1" in masked
    assert "FRIEND_2" in masked
    assert "MEMBER_1" in masked
    assert "GROUP_1" in masked
    assert masker.unmask_text(masked) == raw


def test_masker_handles_add_friend_with_synthetic_int_telegram_id():
    masker = build_chat_masker(speaker="Kiran", friends=[])
    masked = masker.mask_text("add friend Surya 987654321")
    assert "Surya" not in masked
    assert "987654321" not in masked
    assert "FRIEND_1" in masked

    syn_id = int(masked.split()[-1])
    restored_args = masker.unmask_value({"nickname": "FRIEND_1", "telegram_id": syn_id})
    assert restored_args == {"nickname": "Surya", "telegram_id": 987654321}


def test_masker_keeps_short_group_name_safe_from_lowercase_collisions():
    masker = build_chat_masker(speaker="Kiran", group="Trip", members=["Kiran", "Bob"])
    masked = masker.mask_text("split 600 with Bob for our trip")
    assert "Bob" not in masked
    assert "MEMBER_1" in masked
    assert "for our trip" in masked
    assert masker.mask_text("Trip") == "GROUP_1"


def test_unmask_text_handles_double_digit_tokens_without_prefix_overlap():
    masker = PIIMasker()
    tokens = [masker.register_name(f"Person{i}", "FRIEND") for i in range(1, 12)]
    assert tokens[0] == "FRIEND_1" and tokens[9] == "FRIEND_10"
    restored = masker.unmask_text("FRIEND_10 and FRIEND_1")
    assert restored == "Person10 and Person1"


# ------------------------------------------------------------------ Graph integration
@pytest.mark.anyio
async def test_graph_masks_system_and_history_and_unmasks_tool_calls():
    tool_defs = [
        {"name": "split_expense", "description": "split", "parameters": {"type": "object", "properties": {}}},
        {"name": "add_expenses", "description": "add", "parameters": {"type": "object", "properties": {}}},
        {"name": "respond_to_user", "description": "reply", "parameters": {"type": "object", "properties": {}}},
    ]
    llm = CapturingChatLLM(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "split_expense",
                        "args": {
                            "amount": 900,
                            "description": "dinner via [UPI_1]",
                            "with_friends": ["FRIEND_1", "FRIEND_2"],
                        },
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    gw = FakeGateway(
        {
            "speaker": "Kiran",
            "categories": ["food"],
            "friends": ["Vikram", "Neha"],
        }
    )
    graph = build_graph(llm, gw, tool_defs)
    out = await graph.ainvoke(
        {
            "messages": [
                HumanMessage("dinner 900 split with Vikram and Neha via vikram@okicici")
            ]
        },
        config={
            "configurable": {
                "thread_id": "t1",
                "telegram_id": 555666777,
                "name": "Kiran",
                "username": "kiran_tg",
                "chat_type": "private",
            }
        },
    )

    sent = llm.recorded_messages[0]
    sent_blob = "\n".join(str(m.content) for m in sent)
    assert "Vikram" not in sent_blob
    assert "Neha" not in sent_blob
    assert "vikram@okicici" not in sent_blob
    assert "FRIEND_1" in sent_blob
    assert "FRIEND_2" in sent_blob
    assert "[UPI_1]" in sent_blob

    # Tool call sent to MCP server must be unmasked back to real names & UPI ID.
    split_call = next(args for name, args in gw.calls if name == "split_expense")
    assert split_call == {
        "amount": 900,
        "description": "dinner via vikram@okicici",
        "with_friends": ["Vikram", "Neha"],
    }
    assert out["messages"]


@pytest.mark.anyio
async def test_graph_masks_group_context_and_prior_tool_messages():
    tool_defs = [
        {"name": "add_group_expense", "description": "g", "parameters": {"type": "object", "properties": {}}},
        {"name": "respond_to_user", "description": "r", "parameters": {"type": "object", "properties": {}}},
    ]
    llm = CapturingChatLLM(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add_group_expense",
                        "args": {"amount": 500, "description": "cab", "paid_by": "MEMBER_1"},
                        "id": "call_2",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    gw = FakeGateway(
        {
            "speaker": "Kiran Rao",
            "group": "Goa Villa 2026",
            "members": ["Kiran Rao", "Ananya Sharma"],
        }
    )
    graph = build_graph(llm, gw, tool_defs)
    await graph.ainvoke(
        {
            "messages": [
                HumanMessage("Ananya Sharma paid 500 for cab"),
                ToolMessage("Error with Ananya Sharma in Goa Villa 2026", tool_call_id="prev", name="add_group_expense"),
                HumanMessage("try again"),
            ]
        },
        config={
            "configurable": {
                "thread_id": "tg1",
                "telegram_id": 111222333,
                "name": "Kiran Rao",
                "chat_type": "group",
                "group_chat_id": -99887766,
                "group_title": "Goa Villa 2026",
            }
        },
    )
    sent_blob = "\n".join(str(m.content) for m in llm.recorded_messages[0])
    assert "Kiran Rao" not in sent_blob
    assert "Ananya Sharma" not in sent_blob
    assert "Goa Villa 2026" not in sent_blob
    assert "USER_1" in sent_blob
    assert "GROUP_1" in sent_blob
    assert "MEMBER_1" in sent_blob

    call_args = next(args for name, args in gw.calls if name == "add_group_expense")
    assert call_args["paid_by"] == "Ananya Sharma"


# ------------------------------------------------------------------ Statement parser integration
@pytest.mark.anyio
async def test_statement_parser_masks_pii_and_unmasks_descriptions():
    page = (
        "Account Holder: Kiran Rao\n"
        "Account No: 123456789012\n"
        "IFSC: HDFC0001234\n"
        "Email: kiran.rao@gmail.com\n"
        "2026-11-20 UPI/9876543210/surya@okicici/Coffee 150.00 Dr 4850.00"
    )
    fake_llm = CapturingStructuredLLM(
        rows=[
            {
                "date": "2026-11-20",
                "description": "UPI/[PHONE_1]/[UPI_1]/Coffee",
                "amount": 150.0,
                "type": "debit",
                "category": "food",
                "balance_after": 4850.0,
            }
        ]
    )
    rows = await parse_pages(
        fake_llm,
        [page],
        categories=["food", "other"],
        today="2026-11-25",
    )
    sent_text = fake_llm.recorded_messages[0][1].content
    for secret in ("Kiran Rao", "123456789012", "HDFC0001234", "kiran.rao@gmail.com", "9876543210", "surya@okicici"):
        assert secret not in sent_text
    assert "150.00" in sent_text
    assert "2026-11-20" in sent_text
    assert rows[0]["description"] == "UPI/9876543210/surya@okicici/Coffee"


# ------------------------------------------------------------------ Finance agent integration
@pytest.mark.anyio
async def test_finance_agent_masks_company_employee_and_pii_across_llm_calls():
    rules = [
        Rule(id="hotel", name="Hotel stays", keywords=["hotel"], cap_amount=5000, cap_unit="per_night"),
    ]

    # 1. Manager chat
    chat_llm = CapturingFinanceLLM(
        {"on_topic": True, "wants_change": False, "reply": "EMPLOYEE_1 from COMPANY_1 claimed 4000."}
    )
    mgr_reply = await mgr_chat.reply(
        chat_llm,
        999,
        "Acme Corp",
        "- claim #1 from Kiran Rao: Pune trip",
        "Status for Kiran Rao (kiran@acme.com)?",
        "fallback",
        employees=["Kiran Rao"],
    )
    sent_chat = chat_llm.calls[0][1]
    assert "Acme Corp" not in sent_chat
    assert "Kiran Rao" not in sent_chat
    assert "kiran@acme.com" not in sent_chat
    assert mgr_reply == "Kiran Rao from Acme Corp claimed 4000."

    # 2. Policy Q&A
    qa_llm = CapturingFinanceLLM({"kind": "general", "answer": "COMPANY_1 covers up to ₹5,000 for EMPLOYEE_1."})
    qa_res = await policy_qa.answer(
        qa_llm,
        rules,
        1,
        ["Employee: Hi I am Kiran Rao at Acme Corp (9876543210), what is hotel cap?"],
        company="Acme Corp",
        employee="Kiran Rao",
    )
    sent_qa = qa_llm.calls[0][1]
    assert "Kiran Rao" not in sent_qa
    assert "Acme Corp" not in sent_qa
    assert "9876543210" not in sent_qa
    assert "Acme Corp" in qa_res.text and "Kiran Rao" in qa_res.text

    # 3. Claim item classification
    rev_llm = CapturingFinanceLLM({"rule_id": "hotel"})
    matched = await review.rule_for(
        rev_llm,
        rules,
        Item(ref=1, category="travel", description="Stay paid via surya@okicici A/c 123456789012"),
    )
    assert matched is not None and matched.id == "hotel"
    assert "surya@okicici" not in rev_llm.calls[0][1]
    assert "123456789012" not in rev_llm.calls[0][1]

    # 4. Policy parsing
    parse_llm = CapturingFinanceLLM(
        {"rules": [{"id": "meals", "name": "Meals", "conditions": "email [EMAIL_1]"}]}
    )
    parsed = await policy_parser.parse_policy(parse_llm, "Meals cap 500. Contact hr@acme.com")
    assert "hr@acme.com" not in parse_llm.calls[0][1]
    assert parsed[0].conditions == "email hr@acme.com"
