"""Every tool the server offers. Add one: write it in a module with @tool(...) and import the module here."""

from mcp_server.tools import (
    bills,
    budgets,
    conversation,
    expenses,
    friends,
    groups,
    platform,
    reports,
    splits,
    ui,
)  # noqa: F401
from mcp_server.reimbursement import tools as reimbursement_tools  # noqa: F401  (work reimbursements; talks to the Finance Agent over A2A)
