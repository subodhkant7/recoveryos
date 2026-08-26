RECOVERYOS ENGINEERING PRINCIPLE

The system must never confuse:
1. generating a recovery plan,
2. executing a recovery action,
3. verifying that the action succeeded.

Gemini may reason and propose.
Deterministic application code must enforce policy.
Tools execute actions.
Verification determines success.

Never implement fake agentic behavior using hard-coded
if/else responses to specific demo failures.

Never claim an outcome was achieved without verification.

Every workflow must be resumable.

Every externally mutating operation must be idempotent.

Prefer simple architecture over unnecessary abstraction.

The product must demonstrate autonomous background execution,
failure recovery, safe escalation, persistent state and
outcome verification.