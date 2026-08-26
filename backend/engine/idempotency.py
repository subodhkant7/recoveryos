"""
Idempotency Engine & Key Derivation.

Provides deterministic idempotency key derivation and the execution protocol:
1. Derive stable idempotency key from logical operation + target + params.
2. Check local IdempotencyRecord (if SUCCEEDED, return cached result).
3. If not completed locally, query authoritative external system for reconciliation.
4. If external side-effect exists, reconcile local state and return result.
5. If no external side-effect exists, mark EXECUTING, execute external mutation,
   and persist SUCCEEDED / FAILED.
"""

from __future__ import annotations

import json
from typing import Any


def derive_idempotency_key(
    workflow_id: str,
    tool_name: str,
    target_entity_id: str,
    parameters: dict[str, Any] | None = None,
) -> str:
    """
    Deterministically derive a stable idempotency key from logical operation inputs.

    Never includes random values or timestamps.
    Changing operation parameters produces a different key.

    Format:
    idem:{workflow_id}:{tool_name}:{target_entity_id}:{canonical_params}
    """
    # Filter out internal/control arguments that don't affect external side-effects
    # or that are already represented by target_entity_id
    params = parameters or {}
    ignored_keys = {"workflow_id", "step_id", "context", "customer_id"}
    clean_params = {
        k: v for k, v in sorted(params.items())
        if k not in ignored_keys and v is not None and v != ""
    }

    # Format parameter string canonically
    param_str = ",".join(f"{k}={clean_params[k]}" for k in sorted(clean_params.keys()))
    if not param_str:
        param_str = "default"

    return f"idem:{workflow_id}:{tool_name}:{target_entity_id}:{param_str}"
