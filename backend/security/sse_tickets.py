"""
Single-use, short-lived SSE Ticket Management for RecoveryOS.

Provides secure ticket issuance and single-use validation for Server-Sent Events,
eliminating long-lived JWT query parameter exposure in access logs and URLs.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, Optional
from pydantic import BaseModel

from backend.security.principal import Principal, Role


class SSETicket(BaseModel):
    ticket_id: str
    user_id: str
    role: Role
    tenant_id: str
    workflow_id: str
    expires_at: float
    used: bool = False


class SSETicketStore:
    """
    Thread-safe, atomic single-use SSE ticket manager.
    """

    def __init__(self, default_ttl_seconds: int = 60):
        self._tickets: dict[str, SSETicket] = {}
        self._lock = asyncio.Lock()
        self._ttl_seconds = default_ttl_seconds

    async def issue_ticket(
        self,
        principal: Principal,
        workflow_id: str,
        ttl_seconds: int | None = None,
    ) -> SSETicket:
        """Issue a cryptographically random, short-lived, single-use ticket."""
        ttl = ttl_seconds or self._ttl_seconds
        ticket_id = f"sset_{secrets.token_hex(24)}"
        now = time.time()
        ticket = SSETicket(
            ticket_id=ticket_id,
            user_id=principal.user_id,
            role=principal.role,
            tenant_id=principal.tenant_id,
            workflow_id=workflow_id,
            expires_at=now + ttl,
            used=False,
        )
        async with self._lock:
            # Prune expired tickets
            self._prune_expired_locked(now)
            self._tickets[ticket_id] = ticket
        return ticket

    async def consume_ticket(
        self,
        ticket_id: str,
        workflow_id: str,
    ) -> Optional[Principal]:
        """
        Atomically validate and consume a ticket for a specific workflow.
        Returns the authenticated Principal if valid; returns None if invalid,
        expired, mismatched workflow, or already consumed.
        """
        if not ticket_id or not ticket_id.startswith("sset_"):
            return None

        now = time.time()
        async with self._lock:
            ticket = self._tickets.get(ticket_id)
            if not ticket:
                return None

            if ticket.used:
                # Ticket already consumed — reject reuse
                return None

            if now > ticket.expires_at:
                # Ticket expired
                del self._tickets[ticket_id]
                return None

            if ticket.workflow_id != workflow_id:
                # Ticket was issued for a different workflow
                return None

            # Mark ticket as used immediately (single-use)
            ticket.used = True
            # Remove from store to prevent memory leaks
            del self._tickets[ticket_id]

            return Principal(
                user_id=ticket.user_id,
                role=ticket.role,
                tenant_id=ticket.tenant_id,
            )

    def _prune_expired_locked(self, now: float) -> None:
        expired = [tid for tid, t in self._tickets.items() if now > t.expires_at or t.used]
        for tid in expired:
            del self._tickets[tid]

    def clear(self) -> None:
        self._tickets.clear()


# Singleton ticket store instance
sse_ticket_store = SSETicketStore()
