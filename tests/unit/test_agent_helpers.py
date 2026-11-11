"""Pure helpers: pending tool calls, markers, answer folding, runner utilities."""
from backend.agent.graph import pending_tool_calls
from backend.agent.runner import is_rate_limit, message_text
from backend.core.choices import is_cancel, match_option
from backend.core.markers import extract, split_marker, start_marker
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from mcp_server.tools._common import Opt, answer_id, ask_choice


def _ai(**kwargs):
    return AIMessage(content="", **kwargs)


# ---------------------------------------------------------------- pending_tool_calls
def test_no_ai_message_no_calls():
    assert pending_tool_calls([HumanMessage("hi")]) == []


def test_unanswered_calls_listed():
    calls = [{"id": "1", "name": "t", "args": {}}, {"id": "2", "name": "t", "args": {}}]
    msgs = [HumanMessage("hi"), _ai(tool_calls=calls)]
    assert [c["id"] for c in pending_tool_calls(msgs)] == ["1", "2"]


def test_answered_calls_dropped():
    calls = [{"id": "1", "name": "t", "args": {}}, {"id": "2", "name": "t", "args": {}}]
    msgs = [HumanMessage("hi"), _ai(tool_calls=calls),
            ToolMessage("ok", tool_call_id="1", name="t")]
    assert [c["id"] for c in pending_tool_calls(msgs)] == ["2"]


def test_newer_ai_message_wins():
    msgs = [HumanMessage("hi"), _ai(tool_calls=[{"id": "1", "name": "t", "args": {}}]),
            HumanMessage("again"), _ai(tool_calls=[{"id": "2", "name": "t", "args": {}}])]
    assert [c["id"] for c in pending_tool_calls(msgs)] == ["2"]


# ---------------------------------------------------------------- markers
def test_markers_extracted_and_hidden():
    text = f"Split it. {split_marker(5)}\n(above) {start_marker(99887766)}"
    found = extract(text)
    assert found.split_ids == [5]
    assert found.start_ids == [99887766]
    assert "split #" not in found.text and "start #" not in found.text


def test_txn_ids_come_from_expenses_only():
    found = extract("(split #5)", expenses_from="Spent ₹10 on coffee (#12)\n(#13)")
    assert found.txn_ids == [12, 13]
    assert found.split_ids == [5]


# ---------------------------------------------------------------- choices
def test_is_cancel():
    assert is_cancel("cancel")
    assert is_cancel("never mind")
    assert is_cancel("no thanks")
    assert not is_cancel("yes please")
    assert not is_cancel(None)


def test_match_option_folds_words():
    opts = [{"id": "equal", "label": "Equally", "words": ["equally", "same"]},
            {"id": "exact", "label": "I'll tell the amounts", "words": ["exact"]}]
    assert match_option("Equally", opts) == "equal"
    assert match_option("same for me", opts) == "equal"
    assert match_option("exact", opts) == "exact"
    assert match_option("banana", opts) is None


# ---------------------------------------------------------------- HITL helpers
def test_ask_choice_lists_labels():
    text = ask_choice("How?", [Opt("equal", "Equally"), Opt("exact", "Amounts")])
    assert "How?" in text and "Equally" in text and "Amounts" in text and "cancel" in text


def test_ask_choice_marker_roundtrips_into_choices():
    text = ask_choice("How?", [Opt("equal", "Equally"), Opt("exact", "I'll tell the amounts")])
    found = extract(text)
    assert found.choices == [{"id": "equal", "label": "Equally"},
                             {"id": "exact", "label": "I'll tell the amounts"}]
    assert "(ask" not in found.text  # marker hidden from the user (the prose keeps its own parens)


def test_answer_id_folds_words_and_cancels():
    opts = [Opt("equal", "Equally", ("equally",)), Opt("one_off", "One-off", ("once",))]
    assert answer_id("Equally", opts) == "equal"
    assert answer_id("once", opts) == "one_off"
    assert answer_id("cancel", opts) is None
    assert answer_id("banana", opts) is None
    assert answer_id(None, opts) is None


# ---------------------------------------------------------------- runner helpers
def test_message_text_joins_content_blocks():
    assert message_text(AIMessage(content="plain")) == "plain"
    assert message_text(AIMessage(content=[{"text": "a"}, "b", {"text": "c"}])) == "abc"
    assert message_text(AIMessage(content="  ")) == ""


def test_is_rate_limit():
    assert is_rate_limit(Exception("Error code 429"))
    assert is_rate_limit(Exception("ResourceExhausted: quota"))
    assert not is_rate_limit(Exception("connection refused"))
