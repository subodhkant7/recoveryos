"""
Fleet API Routes — Read-only endpoints for the Enterprise Agent Fleet Control Plane.

Provides discovery, inspection, and observability endpoints for the
fleet registry, agent identity, gateway decisions, durable context,
and trace data.

All endpoints are read-only and authenticated.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.security.principal import Principal, Role
from backend.security.dependencies import get_current_principal

from backend.fleet.registry import fleet_registry, AgentStatus
from backend.fleet.identity import identity_from_agent_card
from backend.fleet.gateway import fleet_gateway
from backend.fleet.context_store import fleet_context_store
from backend.fleet.guardrails import fleet_guardrails, GuardrailOutcome
from backend.fleet.observability import fleet_tracer
from backend.fleet.routing import fleet_router as agent_router, ROUTING_TABLE


fleet_api_router = APIRouter(prefix="/api/fleet", tags=["fleet"])


@fleet_api_router.get("/agents")
async def list_fleet_agents(
    department: Optional[str] = None,
    capability: Optional[str] = None,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """
    AUTHENTICATED endpoint: List all registered agents in the fleet.

    Supports filtering by department and capability.
    """
    status_filter = AgentStatus.ACTIVE if not (principal.role == Role.ADMIN) else None
    agents = fleet_registry.list_agents(
        department=department,
        status=status_filter,
        capability=capability,
    )
    # Admins see all agents; others see only ACTIVE
    if principal.role != Role.ADMIN:
        agents = [a for a in agents if a.status == AgentStatus.ACTIVE]

    return {
        "agents": [a.to_card_dict() for a in agents],
        "total": len(agents),
        "departments": list(set(a.department for a in agents)),
    }


@fleet_api_router.get("/agents/{agent_id}")
async def get_fleet_agent(
    agent_id: str,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """
    AUTHENTICATED endpoint: Get a single agent's card and identity.
    """
    agent = fleet_registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{agent_id}' not found in fleet registry",
        )

    identity = identity_from_agent_card(agent)
    return {
        "agent": agent.to_card_dict(),
        "identity": identity.model_dump(mode="json"),
    }


@fleet_api_router.get("/status")
async def fleet_status(
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """
    AUTHENTICATED endpoint: Fleet health summary.
    """
    agents = fleet_registry.list_agents()
    active = sum(1 for a in agents if a.status == AgentStatus.ACTIVE)
    by_dept = fleet_registry.get_agents_by_department()

    return {
        "fleet_status": "OPERATIONAL" if active > 0 else "DEGRADED",
        "total_agents": len(agents),
        "active_agents": active,
        "departments": {
            dept: len(agent_list) for dept, agent_list in by_dept.items()
        },
        "routing_table_size": len(ROUTING_TABLE),
        "data_safety": (
            "Demo data is synthetic. Production-data access is bounded by "
            "tenant, agent identity, tool scope, and policy."
        ),
    }


@fleet_api_router.get("/gateway/check")
async def check_gateway_access(
    agent_id: str = Query(..., description="Agent ID"),
    tool_name: str = Query(..., description="Tool name"),
    tenant_id: str = Query(default="tenant-default", description="Target tenant"),
    data_scope: Optional[str] = Query(default=None, description="Data scope"),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """
    AUTHENTICATED endpoint: Test a gateway access decision without executing.

    Returns the full decision chain for audit and inspection.
    """
    decision = fleet_gateway.evaluate_with_audit(
        agent_id=agent_id,
        tool_name=tool_name,
        tenant_id=tenant_id,
        data_scope=data_scope,
    )
    return decision.model_dump(mode="json")


@fleet_api_router.get("/guardrails/check")
async def check_guardrails(
    agent_id: str = Query(..., description="Agent ID"),
    tool_name: str = Query(..., description="Tool name"),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """
    AUTHENTICATED endpoint: Test guardrail safety checks for an agent/tool pair.
    """
    agent = fleet_registry.get_agent(agent_id)
    allowed_tools = agent.allowed_tools if agent else None
    allowed_scopes = agent.allowed_data_scopes if agent else None

    results = fleet_guardrails.inspect(
        agent_id=agent_id,
        tool_name=tool_name,
        tool_args={},
        agent_allowed_tools=allowed_tools,
        agent_allowed_scopes=allowed_scopes,
    )
    overall = fleet_guardrails.get_overall_outcome(results)

    return {
        "agent_id": agent_id,
        "tool_name": tool_name,
        "overall_outcome": overall.value,
        "checks": [r.model_dump(mode="json") for r in results],
    }


@fleet_api_router.get("/workflows/{workflow_id}/context")
async def get_workflow_context(
    workflow_id: str,
    agent_id: Optional[str] = None,
    scope: Optional[str] = None,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """
    AUTHENTICATED endpoint: Get durable agent context for a workflow.
    """
    snapshot = fleet_context_store.snapshot_context(workflow_id)
    entries = snapshot["entries"]

    if agent_id:
        entries = [e for e in entries if e.get("agent_id") == agent_id]
    if scope:
        entries = [e for e in entries if e.get("scope") == scope]

    return {
        "workflow_id": workflow_id,
        "entries": entries,
        "total": len(entries),
    }


@fleet_api_router.get("/workflows/{workflow_id}/trace")
async def get_workflow_trace(
    workflow_id: str,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """
    AUTHENTICATED endpoint: Get the fleet trace for a workflow.

    Returns OpenTelemetry-compatible structured audit trace events.
    """
    return fleet_tracer.get_trace_summary(workflow_id)


@fleet_api_router.get("/routing")
async def get_routing_table(
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """
    AUTHENTICATED endpoint: Get the agent routing table.
    """
    return {
        "routing_table": agent_router.get_routing_table(),
        "total_routes": len(ROUTING_TABLE),
    }


@fleet_api_router.get("/routing/{outcome_id}")
async def get_route_for_outcome(
    outcome_id: str,
    primary_failed: bool = Query(default=False),
    attempt: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """
    AUTHENTICATED endpoint: Get the routing decision for a specific outcome.
    """
    decision = agent_router.route(
        outcome_id=outcome_id,
        primary_failed=primary_failed,
        attempt=attempt,
    )
    return decision.model_dump(mode="json")
