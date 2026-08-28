"""
Consumer boundary and ingestion handler for asynchronous workflow execution events.

Enforces:
1. Message schema & contract validation.
2. Tenant isolation boundaries.
3. Message replay deduplication via OperationClaim / Idempotency records.
4. Terminal workflow state immutability.
5. OCC expected version verification.
6. Clean delegation to existing WorkflowEngine and AgentFactory.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from backend.events.message_models import (
    WorkflowExecutionMessage,
    WorkflowEventType,
    MessageValidationError,
)
from backend.models.workflow import WorkflowState
from backend.models.idempotency import OperationStatus
from backend.persistence.workflow_store import BaseWorkflowStore, StaleWorkflowStateError
from backend.engine.workflow_engine import WorkflowEngine
from backend.observability.logging import (
    current_request_id,
    current_workflow_id,
    current_tenant_id,
    EVENT_WORKFLOW_CONSUMED,
    EVENT_WORKFLOW_CLAIMED,
    EVENT_WORKFLOW_DUPLICATE,
    EVENT_WORKFLOW_OCC_MISMATCH,
    EVENT_WORKFLOW_TERMINAL_SKIP,
    EVENT_WORKFLOW_EXECUTION_STARTED,
    EVENT_WORKFLOW_EXECUTION_COMPLETED,
)
from backend.observability.metrics import (
    record_occ_mismatch,
    record_duplicate_claim,
)


logger = logging.getLogger("recoveryos.events.consumer")


class ConsumerExecutionError(Exception):
    """Raised when a non-retryable operational violation occurs (e.g. tenant mismatch, missing workflow)."""
    pass


from backend.engine.agent_runner import run_workflow_agent
from backend.agents.agent_factory import AgentFactory


class WorkflowEventConsumer:
    """
    Consumer handler for workflow execution messages.
    Validates contracts and delegates safely to the deterministic engine and agent runner.
    """

    def __init__(
        self,
        store: BaseWorkflowStore,
        engine: WorkflowEngine,
        worker_id: str = "worker-default",
        agent_factory: AgentFactory | None = None,
        test_failure_hook: Callable[[str, WorkflowExecutionMessage], None] | None = None,
    ):
        self._store = store
        self._engine = engine
        self._worker_id = worker_id
        self._agent_factory = agent_factory
        self._test_failure_hook = test_failure_hook

    async def consume_raw_message(self, raw_json: str | bytes) -> dict[str, Any]:
        """
        Validate raw JSON payload and execute workflow event.
        Raises MessageValidationError on invalid payload.
        """
        message = WorkflowExecutionMessage.from_pubsub_json(raw_json)
        return await self.consume_message(message)

    async def consume_message(self, message: WorkflowExecutionMessage) -> dict[str, Any]:
        """
        Process a validated WorkflowExecutionMessage through the complete invariant gate.
        """
        # Set distributed tracing correlation in contextvars for structured logging
        current_request_id.set(message.correlation_id)
        current_workflow_id.set(message.workflow_id)
        current_tenant_id.set(message.tenant_id)

        logger.info(
            "Consuming workflow execution message",
            extra={
                "event_name": EVENT_WORKFLOW_CONSUMED,
                "message_id": message.message_id,
                "event_type": message.event_type.value,
                "workflow_id": message.workflow_id,
                "tenant_id": message.tenant_id,
                "idempotency_key": message.idempotency_key,
                "expected_version": message.expected_version,
            },
        )

        # Optional test hook before claim
        if self._test_failure_hook:
            self._test_failure_hook("before_claim", message)

        # ------------------------------------------------------------------
        # 1. Tenant & Workflow Existence Check
        # ------------------------------------------------------------------
        wf = await self._store.get_workflow(message.workflow_id)
        if not wf:
            logger.warning(
                "Workflow not found for event",
                extra={
                    "event_name": "EVENT_WORKFLOW_NOT_FOUND",
                    "workflow_id": message.workflow_id,
                    "message_id": message.message_id,
                },
            )
            raise ConsumerExecutionError(f"Workflow '{message.workflow_id}' does not exist.")

        wf_tenant = wf.get("tenant_id", "tenant-default")
        if wf_tenant != message.tenant_id:
            logger.error(
                "Cross-tenant message rejection",
                extra={
                    "event_name": "EVENT_TENANT_MISMATCH",
                    "workflow_id": message.workflow_id,
                    "expected_tenant": wf_tenant,
                    "message_tenant": message.tenant_id,
                    "message_id": message.message_id,
                },
            )
            raise ConsumerExecutionError(
                f"Tenant mismatch: Workflow belongs to '{wf_tenant}', message specifies '{message.tenant_id}'."
            )

        # ------------------------------------------------------------------
        # 2. Terminal State Immutability Check
        # ------------------------------------------------------------------
        current_state = wf.get("state")
        if current_state in (WorkflowState.COMPLETED.value, WorkflowState.ESCALATED.value):
            logger.info(
                "Dropping message targeting terminal workflow",
                extra={
                    "event_name": EVENT_WORKFLOW_TERMINAL_SKIP,
                    "workflow_id": message.workflow_id,
                    "state": current_state,
                    "message_id": message.message_id,
                },
            )
            return {
                "status": "SKIPPED_TERMINAL",
                "workflow_id": message.workflow_id,
                "state": current_state,
                "message_id": message.message_id,
            }

        # ------------------------------------------------------------------
        # 3. Distributed Deduplication & Operation Claim Check
        # ------------------------------------------------------------------
        claim_acquired, claim_record = await self._store.claim_operation(
            idempotency_key=message.idempotency_key,
            workflow_id=message.workflow_id,
            tool_name=f"event_{message.event_type.value.lower()}",
            target_entity_id=message.workflow_id,
            parameters={"message_id": message.message_id, "event_type": message.event_type.value},
            worker_id=self._worker_id,
            lease_seconds=60,
        )

        if not claim_acquired:
            claim_status = claim_record.get("status") if claim_record else "UNKNOWN"
            record_duplicate_claim()
            logger.info(
                "Duplicate/Active message claim detected; reusing existing execution",
                extra={
                    "event_name": EVENT_WORKFLOW_DUPLICATE,
                    "idempotency_key": message.idempotency_key,
                    "claim_status": claim_status,
                    "message_id": message.message_id,
                },
            )
            return {
                "status": "SKIPPED_DUPLICATE",
                "workflow_id": message.workflow_id,
                "claim_status": claim_status,
                "message_id": message.message_id,
            }

        logger.info(
            "Operation claim acquired by worker",
            extra={
                "event_name": EVENT_WORKFLOW_CLAIMED,
                "workflow_id": message.workflow_id,
                "idempotency_key": message.idempotency_key,
                "worker_id": self._worker_id,
            },
        )

        # Optional test hook after claim
        if self._test_failure_hook:
            self._test_failure_hook("after_claim", message)

        # ------------------------------------------------------------------
        # 4. OCC Version Check & Execution Transition
        # ------------------------------------------------------------------
        current_version = wf.get("version", 1)
        if current_version != message.expected_version:
            record_occ_mismatch()
            logger.warning(
                "OCC Version mismatch on event consumption",
                extra={
                    "event_name": EVENT_WORKFLOW_OCC_MISMATCH,
                    "workflow_id": message.workflow_id,
                    "current_version": current_version,
                    "expected_version": message.expected_version,
                    "message_id": message.message_id,
                },
            )
            raise StaleWorkflowStateError(
                f"OCC Mismatch: Workflow '{message.workflow_id}' is at version {current_version}, event expected {message.expected_version}"
            )

        # ------------------------------------------------------------------
        # 5. Delegate to Workflow Engine & Agent Execution Runner
        # ------------------------------------------------------------------
        agent_result: dict[str, Any] = {"status": "STATE_ONLY"}
        if message.event_type in (
            WorkflowEventType.WORKFLOW_DISPATCH,
            WorkflowEventType.APPROVAL_RESUME,
            WorkflowEventType.RECOVERY_TRIGGER,
        ):
            if current_state != WorkflowState.EXECUTING.value:
                logger.info(
                    "Transitioning workflow state to EXECUTING",
                    extra={
                        "event_name": EVENT_WORKFLOW_EXECUTION_STARTED,
                        "workflow_id": message.workflow_id,
                        "previous_state": current_state,
                    },
                )
                await self._engine.transition(
                    workflow_id=message.workflow_id,
                    new_state=WorkflowState.EXECUTING,
                    detail=f"Asynchronous worker started execution via event '{message.event_type.value}'",
                    actor=message.producer_id,
                )

            # If AgentFactory is attached, execute the autonomous agent loop with background lease heartbeat
            if self._agent_factory:
                # In distributed multi-process environments (Cloud Run), auto-configure scenario failure injections
                scenario_name = wf.get("scenario")
                services = getattr(self._agent_factory, "services", getattr(self._agent_factory, "_services", None))
                injector = getattr(services, "failure_injector", getattr(services, "_injector", None)) if services else None
                if scenario_name and injector:
                    try:
                        from backend.simulation.scenarios import configure_demo_scenario
                        configure_demo_scenario(injector, message.workflow_id, scenario_name)
                        logger.info(
                            f"Auto-configured scenario '{scenario_name}' on worker",
                            extra={"workflow_id": message.workflow_id, "scenario": scenario_name},
                        )
                    except Exception as e:
                        logger.warning(f"Could not auto-configure scenario {scenario_name}: {e}")

                logger.info(
                    "Executing agent runner in asynchronous worker with lease heartbeat",
                    extra={
                        "workflow_id": message.workflow_id,
                        "worker_id": self._worker_id,
                    },
                )
                heartbeat_stop_event = asyncio.Event()

                async def _lease_heartbeat_loop():
                    while not heartbeat_stop_event.is_set():
                        try:
                            await asyncio.wait_for(heartbeat_stop_event.wait(), timeout=20.0)
                            break
                        except asyncio.TimeoutError:
                            renewed, _ = await self._store.renew_operation_claim(
                                idempotency_key=message.idempotency_key,
                                worker_id=self._worker_id,
                                lease_seconds=60,
                            )
                            if not renewed:
                                logger.error(
                                    "Lease renewal failed; operation ownership lost",
                                    extra={
                                        "event_name": "EVENT_LEASE_RENEWAL_FAILED",
                                        "idempotency_key": message.idempotency_key,
                                        "worker_id": self._worker_id,
                                    },
                                )
                                break

                heartbeat_task = asyncio.create_task(_lease_heartbeat_loop())
                try:
                    agent_result = await run_workflow_agent(
                        workflow_id=message.workflow_id,
                        store=self._store,
                        engine=self._engine,
                        agent_factory=self._agent_factory,
                    )
                finally:
                    heartbeat_stop_event.set()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)

        # Optional test hook after state transition
        if self._test_failure_hook:
            self._test_failure_hook("after_transition", message)

        # Mark the operation claim as COMPLETED so redeliveries are skipped
        await self._store.complete_operation(
            idempotency_key=message.idempotency_key,
            result={"status": "PROCESSED", "message_id": message.message_id, "agent_result": agent_result},
            worker_id=self._worker_id,
        )

        # Optional test hook after completion
        if self._test_failure_hook:
            self._test_failure_hook("after_completion", message)

        logger.info(
            "Workflow execution message processed successfully",
            extra={
                "event_name": "EVENT_PROCESSED",
                "workflow_id": message.workflow_id,
                "message_id": message.message_id,
                "event_type": message.event_type.value,
                "agent_status": agent_result.get("status"),
            },
        )

        return {
            "status": "PROCESSED",
            "workflow_id": message.workflow_id,
            "message_id": message.message_id,
            "event_type": message.event_type.value,
            "tenant_id": message.tenant_id,
            "version": current_version + 1,
            "agent_result": agent_result,
        }
