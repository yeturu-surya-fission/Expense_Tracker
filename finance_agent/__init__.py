"""
The Finance Agent: a separate service that plays a company's finance team.

  policy/   the company's reimbursement policy: rules, the maths that applies them, the LLM that reads and explains them
  claims/   a claim's life: checked against the policy, then decided by the manager
  a2a/      how the expense tracker talks to this agent (the A2A protocol): agent card, skills, authentication, notifications
  manager/  the manager's side in Telegram: set up the policy, answer questions, approve claims
  db/       this service's own database (it never touches the expense tracker's)
"""
