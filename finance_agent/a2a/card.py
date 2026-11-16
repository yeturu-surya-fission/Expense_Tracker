"""The agent card: the public business card at /.well-known/agent-card.json that tells other agents who we are and what we do."""
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol

from finance_agent.config import settings

SKILL_CARDS = [
    AgentSkill(
        id="join_company", name="Join a company", description="An employee joins their company with the code the manager shared.",
        tags=["reimbursement", "onboarding"], examples=["Join FIN-7K2QP"],
    ),
    AgentSkill(
        id="ask_policy", name="Ask about the reimbursement policy",
        description="Answers 'will I get a refund on this?' from the company's stored policy. Asks follow-up questions (city, nights, amount) when needed.",
        tags=["reimbursement", "policy", "question"], examples=["Will I get a refund on a hotel room for 2 nights at 7500?", "What is the meal limit?"],
    ),
    AgentSkill(
        id="ask_manager", name="Ask the manager", description="Passes a question the policy doesn't answer to the manager and returns their answer later.",
        tags=["reimbursement", "escalation"], examples=["Can I claim a gym pass?"],
    ),
    AgentSkill(
        id="submit_claim", name="Submit an expense claim",
        description="Checks each claimed item against the policy, asks for missing details, then hands the claim to the manager, who makes the final decision.",
        tags=["reimbursement", "claim"], examples=["Claim my Pune trip: cab 450, hotel 15000 for 2 nights, dinner 1400"],
    ),
    AgentSkill(id="claim_reply", name="Reply on a claim", description="Gives missing details, or answers the manager's question about a claim.", tags=["reimbursement", "claim"]),
    AgentSkill(id="claim_status", name="Claim status", description="Where a claim stands.", tags=["reimbursement", "claim"]),
]


def build_card() -> AgentCard:
    return AgentCard(
        name="Finance Agent",
        description="A company's finance team as an agent: holds the reimbursement policy, answers policy questions, checks expense claims and asks the manager for the final approval.",
        version="1.0.0",
        supported_interfaces=[AgentInterface(url=settings.audience, protocol_binding=TransportProtocol.JSONRPC.value, protocol_version=PROTOCOL_VERSION_1_0)],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        skills=SKILL_CARDS,
    )
