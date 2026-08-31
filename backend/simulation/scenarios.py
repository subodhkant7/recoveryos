"""
ACME Corp Onboarding — Scenario Definitions.

Defines the OutcomeContract and scenario configurations for
the ACME Corp customer onboarding demo.
"""

from __future__ import annotations

from backend.models.workflow import Constraint, OutcomeContract, RequiredOutcome
from backend.simulation.failure_injector import (
    FailureInjector,
    configure_scenario_1,
    configure_scenario_2,
    configure_scenario_3,
)


def create_acme_contract(workflow_id: str) -> dict:
    """
    Create the OutcomeContract for ACME Corp onboarding.

    This defines WHAT must be true when the workflow completes.
    The agent is free to choose any path that achieves these
    outcomes while respecting the constraints.
    """
    contract = OutcomeContract(
        workflow_id=workflow_id,
        required_outcomes=[
            RequiredOutcome(
                outcome_id="identity_verified",
                description="Customer identity confirmed against government records",
                acceptance_criteria={
                    "verification_level": "standard",
                    "id_type": "government",
                },
                verification_method="query_identity_service_by_customer_id",
                required_evidence=["verification_reference_id"],
            ),
            RequiredOutcome(
                outcome_id="documents_validated",
                description="Business registration documents validated",
                acceptance_criteria={
                    "document_types": ["incorporation", "tax_id"],
                },
                verification_method="query_document_service_by_submission_id",
                required_evidence=["validation_reference_id"],
            ),
            RequiredOutcome(
                outcome_id="risk_assessed",
                description="Customer risk score computed and within acceptable range",
                acceptance_criteria={
                    "max_risk_score": 75,
                },
                verification_method="query_risk_service_by_customer_id",
                required_evidence=["risk_score", "assessment_id"],
            ),
            RequiredOutcome(
                outcome_id="billing_configured",
                description="Active billing subscription matching requested plan",
                acceptance_criteria={
                    "plan_tier": "enterprise",
                    "billing_cycle": "monthly",
                },
                verification_method="query_billing_service_for_active_subscription",
                required_evidence=["subscription_id", "provider", "plan_tier"],
            ),
            RequiredOutcome(
                outcome_id="account_activated",
                description="Customer account is active and accessible",
                acceptance_criteria={
                    "status": "active",
                },
                verification_method="query_account_service_by_customer_id",
                required_evidence=["account_id", "activation_timestamp"],
            ),
            RequiredOutcome(
                outcome_id="welcome_sent",
                description="Welcome package delivered to customer",
                acceptance_criteria={
                    "delivery_status": "delivered",
                },
                verification_method="query_notification_service_by_message_id",
                required_evidence=["message_id", "delivery_timestamp"],
            ),
        ],
        constraints=[
            Constraint(
                constraint_id="identity_first",
                description="Identity must be verified before any other step",
                enforcement="policy",
            ),
            Constraint(
                constraint_id="risk_before_billing",
                description="Risk assessment must pass before billing is configured",
                enforcement="policy",
            ),
            Constraint(
                constraint_id="single_billing_provider",
                description="Customer must be billed through exactly one provider",
                enforcement="verification",
            ),
        ],
        prohibited_outcomes=[
            "double_charge",
            "account_activated_without_billing",
            "billing_configured_for_wrong_plan",
        ],
    )
    return contract.model_dump(mode="json")


ACME_CUSTOMER_DATA = {
    "customer_id": "acme-001",
    "company_name": "ACME Corp",
    "full_name": "Alice Acme",
    "email": "alice@acmecorp.com",
    "requested_plan": "enterprise",
    "billing_cycle": "monthly",
    "preferred_billing_provider": "stripe",
}


def configure_demo_scenario(
    injector: FailureInjector,
    workflow_id: str,
    scenario_name: str,
    services: Any | None = None,
    reset_state: bool = False,
) -> None:
    """Configure failure injection and isolated service state for a named demo scenario."""
    if services is not None and hasattr(services, "configure_billing_provider"):
        if reset_state and hasattr(services, "reset_for_workflow"):
            services.reset_for_workflow(workflow_id)
        if scenario_name == "billing_unavailable":
            services.configure_billing_provider("stripe", status="down")
        else:
            services.configure_billing_provider("stripe", status="healthy")
            services.configure_billing_provider("paypal", status="healthy")

    scenarios = {
        "billing_unavailable": configure_scenario_1,
        "contradictory_evidence": configure_scenario_2,
        "worker_interruption": configure_scenario_3,
    }
    setup_fn = scenarios.get(scenario_name)
    if setup_fn:
        setup_fn(injector, workflow_id)
    else:
        raise ValueError(f"Unknown scenario: {scenario_name}. Available: {list(scenarios.keys())}")
