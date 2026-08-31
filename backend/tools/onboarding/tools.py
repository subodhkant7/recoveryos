"""
ADK Agent Tools — Onboarding operations.

These are FunctionTools that the Taskmaster agent can call.
Each tool:
1. Passes through the policy engine (via before_tool_callback)
2. Enforces deterministic idempotency and crash reconciliation
3. Tracks step lifecycle (START -> RUNNING -> COMPLETED / FAILED) via workflow engine
4. Delegates to the simulated external service
5. Records immutable evidence
6. For verification: updates verified state on the workflow's OutcomeContract
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from backend.models.evidence import Evidence, EvidenceType, VerificationResult
from backend.models.events import EventType
from backend.models.idempotency import IdempotencyStatus
from backend.models.recovery import (
    RecoveryPlan,
    RecoveryPlanStatus,
    RecoveryStep,
    Confidence,
)
from backend.models.workflow import WorkflowState
from backend.engine.idempotency import derive_idempotency_key
from backend.simulation.failure_injector import CrashBeforePersistenceError


class OnboardingTools:
    """
    Tool implementations for the ACME onboarding workflow.

    Initialized with shared dependencies (services, store, engine).
    Each method becomes an ADK FunctionTool via the agent definition.
    """

    def __init__(self, services, store, engine):
        self._services = services
        self._store = store
        self._engine = engine

    # ------------------------------------------------------------------
    # Idempotent Step Execution Protocol
    # ------------------------------------------------------------------

    async def _execute_step(
        self,
        workflow_id: str,
        name: str,
        tool_name: str,
        target_entity_id: str,
        tool_args: dict[str, Any],
        target_outcome_id: str | None,
        action_fn: Callable[[str], Any],
        evidence_source: str,
        evidence_type: EvidenceType = EvidenceType.TOOL_RESULT,
        worker_id: str = "worker-default",
    ) -> dict[str, Any]:
        """
        Execute an externally mutating tool action with distributed operation claiming,
        idempotency, and ground-truth crash reconciliation.
        """
        # 1. Guard against terminal workflow mutation
        wf = await self._store.get_workflow(workflow_id)
        if wf and wf.get("state") in (WorkflowState.COMPLETED.value, WorkflowState.ESCALATED.value):
            return {
                "status": "error",
                "error_type": "TERMINAL_STATE_ERROR",
                "message": f"Cannot execute mutations on workflow in terminal state '{wf.get('state')}'",
            }

        # 2. Derive canonical idempotency key
        idempotency_key = derive_idempotency_key(
            workflow_id=workflow_id,
            tool_name=tool_name,
            target_entity_id=target_entity_id,
            parameters=tool_args,
        )

        lock = self._store.get_lock(idempotency_key)
        async with lock:
            # 3. Check local idempotency record
            cached = await self._store.get_idempotency_record(idempotency_key)
            if cached and cached.get("status") in (IdempotencyStatus.SUCCEEDED.value, "COMPLETED"):
                return cached.get("result", {})

            # 4. Transactionally claim operation lease
            acquired, claim = await self._store.claim_operation(
                idempotency_key=idempotency_key,
                workflow_id=workflow_id,
                tool_name=tool_name,
                target_entity_id=target_entity_id,
                parameters=tool_args,
                worker_id=worker_id,
                lease_seconds=60,
            )

            if not acquired:
                if claim.get("status") == "COMPLETED" and claim.get("result"):
                    return claim.get("result", {})
                # Active lease held by another worker: do not execute duplicate mutation
                return {
                    "status": "in_progress",
                    "message": f"Operation '{idempotency_key}' is actively leased by worker '{claim.get('owner_worker_id')}'",
                    "operation_id": claim.get("operation_id"),
                }

            # 5. Pre-Mutation Ground-Truth Check (Authoritative External Reconciliation)
            kwargs_clean = {k: v for k, v in tool_args.items() if k != "customer_id"}
            external_record = await self._services.check_external_mutation(
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                customer_id=target_entity_id,
                workflow_id=workflow_id,
                **kwargs_clean,
            )
            if external_record is not None:
                # Reconcile local state from authoritative external record
                reconciled_result = {**external_record}
                evidence_id = await self._record_evidence(
                    workflow_id, evidence_source, evidence_type, reconciled_result
                )
                step_id = str(uuid.uuid4())
                step_data = {
                    "step_id": step_id,
                    "workflow_id": workflow_id,
                    "name": f"{name} (Reconciled)",
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "target_outcome_id": target_outcome_id,
                    "idempotency_key": idempotency_key,
                    "status": "COMPLETED",
                }
                await self._engine.record_step_started(workflow_id, step_data)
                await self._engine.record_step_completed(
                    workflow_id, step_id, result=reconciled_result, evidence_id=evidence_id
                )
                await self._store.complete_operation(
                    idempotency_key=idempotency_key,
                    result=reconciled_result,
                    worker_id=worker_id,
                )
                return reconciled_result

            # 6. Mark local state as EXECUTING
            step_id = str(uuid.uuid4())
            step_data = {
                "step_id": step_id,
                "workflow_id": workflow_id,
                "name": name,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "target_outcome_id": target_outcome_id,
                "idempotency_key": idempotency_key,
                "status": "PENDING",
                "attempt_count": 0,
            }
            await self._engine.record_step_started(workflow_id, step_data)

            idem_rec = {
                "idempotency_key": idempotency_key,
                "workflow_id": workflow_id,
                "tool_name": tool_name,
                "target_entity_id": target_entity_id,
                "parameters": tool_args,
                "status": IdempotencyStatus.EXECUTING.value,
                "step_id": step_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await self._store.save_idempotency_record(idempotency_key, idem_rec)

            try:
                # 7. Execute mutation against external service
                result = await action_fn(idempotency_key)

                # Simulated crash hook (tests crash after success before local persistence)
                if self._services._injector.should_crash_after_external_success(
                    workflow_id, tool_name
                ):
                    raise CrashBeforePersistenceError(
                        f"Simulated crash after external mutation success for {tool_name}"
                    )

                # Check if service returned an error response
                if isinstance(result, dict) and result.get("status") == "error":
                    error_msg = result.get("message") or result.get("error_type") or "Service returned error"
                    evidence_id = await self._record_evidence(
                        workflow_id, evidence_source, EvidenceType.FAILURE_DETAIL, result
                    )
                    failure_data = {
                        "failure_id": str(uuid.uuid4()),
                        "workflow_id": workflow_id,
                        "step_id": step_id,
                        "error_type": result.get("error_type", "TOOL_ERROR"),
                        "error_detail": error_msg,
                        "raw_error": result,
                        "evidence_ids": [evidence_id],
                        "diagnosed": False,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    await self._engine.record_step_failed(
                        workflow_id, step_id, error=error_msg, failure_data=failure_data
                    )
                    await self._store.fail_operation(
                        idempotency_key=idempotency_key,
                        error=error_msg,
                        worker_id=worker_id,
                    )
                    return result

                # 8. Success: record evidence & mark operation completed
                evidence_id = await self._record_evidence(
                    workflow_id, evidence_source, evidence_type, result
                )
                await self._engine.record_step_completed(
                    workflow_id, step_id, result=result, evidence_id=evidence_id
                )
                await self._store.complete_operation(
                    idempotency_key=idempotency_key,
                    result=result,
                    worker_id=worker_id,
                )
                return result

            except CrashBeforePersistenceError:
                # Re-raise test-simulated crash
                raise

            except Exception as e:
                error_msg = str(e)
                evidence_id = await self._record_evidence(
                    workflow_id, evidence_source, EvidenceType.FAILURE_DETAIL,
                    {"error": error_msg, "exception_type": type(e).__name__}
                )
                failure_data = {
                    "failure_id": str(uuid.uuid4()),
                    "workflow_id": workflow_id,
                    "step_id": step_id,
                    "error_type": "TOOL_ERROR",
                    "error_detail": error_msg,
                    "raw_error": {"exception": error_msg},
                    "evidence_ids": [evidence_id],
                    "diagnosed": False,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                await self._engine.record_step_failed(
                    workflow_id, step_id, error=error_msg, failure_data=failure_data
                )
                await self._store.fail_operation(
                    idempotency_key=idempotency_key,
                    error=error_msg,
                    worker_id=worker_id,
                )
                raise

    # ------------------------------------------------------------------
    # Mutating Tools (these change external state)
    # ------------------------------------------------------------------

    async def verify_identity(
        self, workflow_id: str, customer_id: str, full_name: str = "",
        id_type: str = "government",
    ) -> dict[str, Any]:
        """
        Verify customer identity against government records.
        """
        args = {"customer_id": customer_id, "full_name": full_name, "id_type": id_type}
        return await self._execute_step(
            workflow_id=workflow_id,
            name="Verify Customer Identity",
            tool_name="verify_identity",
            target_entity_id=customer_id,
            tool_args=args,
            target_outcome_id="identity_verified",
            action_fn=lambda idem_key: self._services.verify_identity(
                workflow_id=workflow_id,
                customer_id=customer_id,
                full_name=full_name,
                id_type=id_type,
                idempotency_key=idem_key,
            ),
            evidence_source="verify_identity",
            evidence_type=EvidenceType.TOOL_RESULT,
        )

    async def validate_documents(
        self, workflow_id: str, customer_id: str,
        document_types: str = "incorporation,tax_id",
    ) -> dict[str, Any]:
        """
        Validate business registration documents via OCR/document service.
        """
        types_list = [t.strip() for t in document_types.split(",")]
        args = {"customer_id": customer_id, "document_types": document_types}
        return await self._execute_step(
            workflow_id=workflow_id,
            name="Validate Business Documents",
            tool_name="validate_documents",
            target_entity_id=customer_id,
            tool_args=args,
            target_outcome_id="documents_validated",
            action_fn=lambda idem_key: self._services.validate_documents(
                workflow_id=workflow_id,
                customer_id=customer_id,
                document_types=types_list,
                idempotency_key=idem_key,
            ),
            evidence_source="validate_documents",
            evidence_type=EvidenceType.TOOL_RESULT,
        )

    async def run_risk_check(
        self, workflow_id: str, customer_id: str,
    ) -> dict[str, Any]:
        """
        Run credit/risk assessment on the customer.
        """
        args = {"customer_id": customer_id}
        return await self._execute_step(
            workflow_id=workflow_id,
            name="Assess Customer Risk",
            tool_name="run_risk_check",
            target_entity_id=customer_id,
            tool_args=args,
            target_outcome_id="risk_assessed",
            action_fn=lambda idem_key: self._services.run_risk_check(
                workflow_id=workflow_id,
                customer_id=customer_id,
                idempotency_key=idem_key,
            ),
            evidence_source="run_risk_check",
            evidence_type=EvidenceType.TOOL_RESULT,
        )

    async def setup_billing(
        self, workflow_id: str, customer_id: str,
        provider: str = "stripe",
        plan_tier: str = "enterprise",
        billing_cycle: str = "monthly",
    ) -> dict[str, Any]:
        """
        Configure billing subscription for the customer.
        """
        args = {
            "customer_id": customer_id,
            "provider": provider,
            "plan_tier": plan_tier,
            "billing_cycle": billing_cycle,
        }
        return await self._execute_step(
            workflow_id=workflow_id,
            name=f"Configure Billing ({provider})",
            tool_name="setup_billing",
            target_entity_id=customer_id,
            tool_args=args,
            target_outcome_id="billing_configured",
            action_fn=lambda idem_key: self._services.setup_billing(
                workflow_id=workflow_id,
                customer_id=customer_id,
                provider=provider,
                plan_tier=plan_tier,
                billing_cycle=billing_cycle,
                idempotency_key=idem_key,
            ),
            evidence_source=f"setup_billing:{provider}",
            evidence_type=EvidenceType.TOOL_RESULT,
        )

    async def activate_account(
        self, workflow_id: str, customer_id: str,
    ) -> dict[str, Any]:
        """
        Activate the customer's account.
        """
        args = {"customer_id": customer_id}
        return await self._execute_step(
            workflow_id=workflow_id,
            name="Activate Account",
            tool_name="activate_account",
            target_entity_id=customer_id,
            tool_args=args,
            target_outcome_id="account_activated",
            action_fn=lambda idem_key: self._services.activate_account(
                workflow_id=workflow_id,
                customer_id=customer_id,
                idempotency_key=idem_key,
            ),
            evidence_source="activate_account",
            evidence_type=EvidenceType.TOOL_RESULT,
        )

    async def send_welcome_package(
        self, workflow_id: str, customer_id: str, email: str = "",
    ) -> dict[str, Any]:
        """
        Send welcome package/email to the customer.
        """
        args = {"customer_id": customer_id, "email": email}
        return await self._execute_step(
            workflow_id=workflow_id,
            name="Send Welcome Package",
            tool_name="send_welcome_package",
            target_entity_id=customer_id,
            tool_args=args,
            target_outcome_id="welcome_sent",
            action_fn=lambda idem_key: self._services.send_welcome_package(
                workflow_id=workflow_id,
                customer_id=customer_id,
                email=email,
                idempotency_key=idem_key,
            ),
            evidence_source="send_welcome_package",
            evidence_type=EvidenceType.TOOL_RESULT,
        )

    # ------------------------------------------------------------------
    # Read-Only Diagnostic Tools (no state mutation)
    # ------------------------------------------------------------------

    async def check_service_status(self, service_name: str) -> dict[str, Any]:
        """
        Check the health/availability of an external service.
        """
        return self._services.get_service_status(service_name)

    async def list_available_billing_providers(self) -> list[dict[str, Any]]:
        """
        Discover available billing providers and their capabilities.
        """
        return self._services.list_available_billing_providers()

    async def get_workflow_state(self, workflow_id: str) -> dict[str, Any]:
        """
        Get the current state of a workflow including all steps and evidence.
        """
        snapshot = await self._store.get_workflow_snapshot(workflow_id)
        if not snapshot:
            return {"status": "not_found", "workflow_id": workflow_id}
        return snapshot

    # ------------------------------------------------------------------
    # Verification Tools (independent confirmation & contract state update)
    # ------------------------------------------------------------------

    async def verify_outcome(
        self, workflow_id: str, outcome_id: str, customer_id: str,
    ) -> dict[str, Any]:
        """
        Independently verify that a specific outcome was achieved.

        This makes a SEPARATE query to the target service — it does NOT
        rely on the tool result. A tool saying "success" does not count
        as verification. Only this method's independent confirmation counts.

        When verified, it updates the contract state in durable storage.
        """
        step_id = str(uuid.uuid4())
        step_data = {
            "step_id": step_id,
            "workflow_id": workflow_id,
            "name": f"Verify Outcome: {outcome_id}",
            "tool_name": "verify_outcome",
            "tool_args": {"outcome_id": outcome_id, "customer_id": customer_id},
            "target_outcome_id": outcome_id,
            "status": "PENDING",
            "attempt_count": 0,
        }
        await self._engine.record_step_started(workflow_id, step_data)

        # Load workflow to get contract and acceptance criteria
        wf = await self._store.get_workflow(workflow_id)
        if not wf:
            err_msg = f"Workflow '{workflow_id}' not found"
            await self._engine.record_step_failed(workflow_id, step_id, error=err_msg)
            return {"status": "error", "message": err_msg}

        from backend.models.workflow import normalize_contract
        contract = normalize_contract(wf.get("contract", {}), workflow_id=workflow_id)
        outcomes = contract.get("required_outcomes", [])
        target = next((o for o in outcomes if isinstance(o, dict) and o.get("outcome_id") == outcome_id), None)

        if not target:
            err_msg = f"Outcome '{outcome_id}' not found in contract"
            await self._engine.record_step_failed(workflow_id, step_id, error=err_msg)
            return {"status": "error", "message": err_msg}

        criteria = target.get("acceptance_criteria", {})

        # Perform independent verification query
        verification_data = await self._perform_verification(
            outcome_id, customer_id, criteria
        )
        verification_data["workflow_id"] = workflow_id

        # Record verification evidence
        evidence_id = await self._record_evidence(
            workflow_id,
            f"verify:{outcome_id}",
            EvidenceType.VERIFICATION,
            verification_data,
        )

        passed = verification_data.get("passed", False)
        discrepancies = verification_data.get("discrepancies", [])

        # Update contract state
        target["verified"] = passed
        evidence_ids = target.setdefault("evidence_ids", [])
        if evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)

        # Persist updated contract in workflow
        wf["contract"] = contract
        await self._store.save_workflow(wf)

        # Record verification event
        await self._engine._record_event(
            workflow_id=workflow_id,
            event_type=EventType.VERIFICATION_RESULT,
            title=f"Verification {'Passed' if passed else 'Failed'}: {outcome_id}",
            detail=f"Method: {verification_data.get('method')}; Discrepancies: {discrepancies}",
            payload={
                "outcome_id": outcome_id,
                "passed": passed,
                "evidence_id": evidence_id,
                "discrepancies": discrepancies,
            },
            actor="verifier",
        )

        # Record Fleet Verification trace and durable context
        try:
            from backend.fleet.observability import fleet_tracer
            from backend.fleet.context_store import fleet_context_store
            fleet_tracer.record_event(
                workflow_id=workflow_id,
                agent_id="verification-agent",
                event_type="OUTCOME_VERIFICATION",
                tool="verify_outcome",
                outcome="PASSED" if passed else "FAILED",
                detail=f"Independent verification {'passed' if passed else 'failed'} for {outcome_id}",
                metadata={"outcome_id": outcome_id, "passed": passed, "evidence_id": evidence_id},
            )
            fleet_context_store.save_context(
                workflow_id=workflow_id,
                agent_id="verification-agent",
                key=f"verified_{outcome_id}",
                value={"passed": passed, "discrepancies": discrepancies, "evidence_id": evidence_id},
                scope="verification",
            )
        except Exception:
            pass

        # Update associated recovery plans
        plans = await self._store.get_recovery_plans(workflow_id)
        for p in plans:
            if p.get("target_outcome_id") == outcome_id and p.get("status") in (
                RecoveryPlanStatus.PROPOSED.value,
                RecoveryPlanStatus.APPROVED.value,
                RecoveryPlanStatus.EXECUTING.value,
            ):
                p["status"] = RecoveryPlanStatus.SUCCEEDED.value if passed else RecoveryPlanStatus.FAILED.value
                await self._store.save_recovery_plan(workflow_id, p)

        if passed:
            await self._engine.record_step_completed(
                workflow_id, step_id, result=verification_data, evidence_id=evidence_id
            )
        else:
            is_contradictory = any(
                "contradictory" in str(d).lower() or ("plan" in str(d).lower() and ("expected" in str(d).lower() or "got" in str(d).lower()))
                for d in discrepancies
            )

            failure_data = {
                "failure_id": str(uuid.uuid4()),
                "workflow_id": workflow_id,
                "step_id": step_id,
                "error_type": "CONTRADICTORY_EVIDENCE" if is_contradictory else "VERIFICATION_FAILED",
                "error_detail": "; ".join(discrepancies) or f"Verification failed for outcome {outcome_id}",
                "raw_error": verification_data,
                "evidence_ids": [evidence_id],
                "diagnosed": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await self._engine.record_step_failed(
                workflow_id,
                step_id,
                error="; ".join(discrepancies) or f"Verification failed for {outcome_id}",
                failure_data=failure_data,
            )

            if is_contradictory:
                # Deterministic deduplication key for approval request
                from backend.models.approval import HumanApproval, ApprovalStatus
                dedup_key = derive_idempotency_key(
                    workflow_id=workflow_id,
                    tool_name="verify_outcome",
                    target_entity_id=customer_id,
                    parameters={"outcome_id": outcome_id, "discrepancies": discrepancies},
                )
                existing = await self._store.get_pending_approval_by_dedup(workflow_id, dedup_key)
                if not existing:
                    approval = HumanApproval(
                        workflow_id=workflow_id,
                        action_tool="verify_outcome",
                        action_args={"outcome_id": outcome_id, "customer_id": customer_id},
                        policy_rule="evidence_consistency",
                        reason=f"Verification detected contradictory evidence: {'; '.join(discrepancies)}",
                        title=f"Approval Required: Contradictory Evidence in {outcome_id}",
                        description=f"Action result contradicted by independent verification. Human authorization required to proceed.",
                        dedup_key=dedup_key,
                        status=ApprovalStatus.PENDING,
                        evidence_ids=[evidence_id],
                    )
                    await self._store.save_approval(workflow_id, approval.model_dump(mode="json"))

                    await self._engine._record_event(
                        workflow_id=workflow_id,
                        event_type=EventType.APPROVAL_REQUESTED,
                        title=f"Approval Requested: Contradictory Evidence in {outcome_id}",
                        detail=f"Verification detected contradictory evidence: {'; '.join(discrepancies)}",
                        payload={
                            "approval_id": approval.approval_id,
                            "outcome_id": outcome_id,
                            "discrepancies": discrepancies,
                            "policy_rule": "evidence_consistency",
                        },
                        actor="verifier",
                    )

                # Halt workflow autonomy: pause in AWAITING_APPROVAL
                wf_cur = await self._store.get_workflow(workflow_id)
                cur_state = wf_cur.get("state") if wf_cur else None
                if cur_state == WorkflowState.CREATED.value:
                    await self._engine.transition(
                        workflow_id,
                        WorkflowState.EXECUTING,
                        detail="Transitioning CREATED to EXECUTING before approval pause",
                        actor="verifier",
                    )
                await self._engine.transition(
                    workflow_id,
                    WorkflowState.AWAITING_APPROVAL,
                    detail=f"Paused: Contradictory evidence detected in {outcome_id} ({'; '.join(discrepancies)})",
                    actor="verifier",
                )

        return verification_data

    async def _perform_verification(
        self, outcome_id: str, customer_id: str, criteria: dict,
    ) -> dict[str, Any]:
        """
        Execute the independent verification query for each outcome type.

        Queries the service directly to verify actual state against acceptance criteria.
        """
        if outcome_id == "identity_verified":
            result = await self._services.query_identity_status(customer_id)
            passed = result.get("found", False)
            discrepancies = []
            if not passed:
                discrepancies.append(f"Identity record not found for customer '{customer_id}'")
            return self._build_verification(
                outcome_id, passed, result, criteria,
                method="Independent query to identity service by customer_id",
                discrepancies=discrepancies,
            )

        elif outcome_id == "documents_validated":
            result = await self._services.query_document_status(customer_id)
            passed = result.get("found", False)
            discrepancies = []
            if not passed:
                discrepancies.append(f"Document validation record not found for customer '{customer_id}'")
            return self._build_verification(
                outcome_id, passed, result, criteria,
                method="Independent query to document service by customer_id",
                discrepancies=discrepancies,
            )

        elif outcome_id == "risk_assessed":
            result = await self._services.query_risk_status(customer_id)
            passed = result.get("found", False)
            discrepancies = []
            if not passed:
                discrepancies.append(f"Risk assessment not found for customer '{customer_id}'")
            else:
                max_score = criteria.get("max_risk_score", 100)
                actual_score = result.get("risk_score", 100)
                if actual_score > max_score:
                    passed = False
                    discrepancies.append(f"Risk score {actual_score} exceeds maximum allowed {max_score}")
            return self._build_verification(
                outcome_id, passed, result, criteria,
                method="Independent query to risk service by customer_id",
                discrepancies=discrepancies,
            )

        elif outcome_id == "billing_configured":
            result = await self._services.query_billing_status(customer_id)
            passed = result.get("found", False)
            discrepancies = []
            if not passed:
                discrepancies.append(f"Billing subscription not found for customer '{customer_id}'")
            else:
                if criteria.get("plan_tier") and result.get("plan_tier") != criteria["plan_tier"]:
                    discrepancies.append(
                        f"Expected plan_tier={criteria['plan_tier']}, got {result.get('plan_tier')}"
                    )
                    passed = False
                if criteria.get("billing_cycle") and result.get("billing_cycle") != criteria["billing_cycle"]:
                    discrepancies.append(
                        f"Expected billing_cycle={criteria['billing_cycle']}, got {result.get('billing_cycle')}"
                    )
                    passed = False
            return self._build_verification(
                outcome_id, passed, result, criteria,
                method="Independent query to billing service for active subscription",
                discrepancies=discrepancies,
            )

        elif outcome_id == "account_activated":
            result = await self._services.query_account_status(customer_id)
            passed = result.get("found", False)
            discrepancies = []
            if not passed:
                discrepancies.append(f"Account not found for customer '{customer_id}'")
            else:
                actual_status = result.get("account_status") or result.get("status")
                if criteria.get("status") and actual_status != criteria["status"]:
                    discrepancies.append(
                        f"Expected account status={criteria['status']}, got {actual_status}"
                    )
                    passed = False
            return self._build_verification(
                outcome_id, passed, result, criteria,
                method="Independent query to account service by customer_id",
                discrepancies=discrepancies,
            )

        elif outcome_id == "welcome_sent":
            passed = False
            record_found = None
            for record in self._services._notification_records.values():
                if record.get("customer_id") == customer_id:
                    record_found = record
                    expected_status = criteria.get("delivery_status", "delivered")
                    actual_status = record.get("delivery_status")
                    passed = (actual_status == expected_status)
                    break

            discrepancies = []
            if not record_found:
                discrepancies.append(f"Welcome notification not found for customer '{customer_id}'")
            elif not passed:
                discrepancies.append(
                    f"Expected delivery_status={criteria.get('delivery_status')}, got {record_found.get('delivery_status')}"
                )

            return self._build_verification(
                outcome_id, passed, record_found or {"status": "not_found"}, criteria,
                method="Independent query to notification service by customer record",
                discrepancies=discrepancies,
            )

        return {"status": "error", "message": f"No verification method for outcome '{outcome_id}'"}

    def _build_verification(
        self,
        outcome_id: str,
        passed: bool,
        evidence_data: dict,
        criteria: dict,
        method: str,
        discrepancies: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build a standardized verification result dictionary."""
        result = VerificationResult(
            workflow_id="",
            target=outcome_id,
            target_type="outcome",
            method=method,
            passed=passed,
            evidence=evidence_data,
            discrepancies=discrepancies or [],
        )
        d = result.model_dump(mode="json")
        d["outcome_id"] = outcome_id
        return d

    # ------------------------------------------------------------------
    # Recovery Planning Tools
    # ------------------------------------------------------------------

    async def submit_recovery_plan(
        self,
        workflow_id: str,
        target_outcome_id: str,
        diagnosis: str,
        proposed_steps: list[dict[str, Any]],
        root_cause: str = "",
        objective: str = "",
        constraints: list[str] | None = None,
        expected_evidence: dict[str, Any] | None = None,
        verification_strategy: str = "",
        risks: list[str] | None = None,
        reasoning: str = "",
        confidence: str = "MEDIUM",
        rollback_notes: str = "",
        evidence_ids: list[str] | None = None,
        failure_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Submit and persist a structured RecoveryPlan.

        Validates the proposed recovery plan against the OutcomeContract,
        known tool capabilities, and prerequisite constraints before persisting.
        """
        # 1. Load workflow
        wf = await self._store.get_workflow(workflow_id)
        if not wf:
            return {"status": "error", "error_type": "VALIDATION_ERROR", "message": f"Workflow '{workflow_id}' not found"}

        current_state = wf.get("state")
        if current_state in (WorkflowState.COMPLETED.value, WorkflowState.ESCALATED.value):
            return {
                "status": "error",
                "error_type": "TERMINAL_STATE_ERROR",
                "message": f"Cannot submit recovery plan for workflow in terminal state '{current_state}'",
            }

        from backend.models.workflow import normalize_contract
        contract = normalize_contract(wf.get("contract", {}), workflow_id=workflow_id)
        required_outcomes = [(o.get("outcome_id") if isinstance(o, dict) else str(o)) for o in contract.get("required_outcomes", [])]

        # 2. Validate target outcome
        if target_outcome_id not in required_outcomes:
            return {
                "status": "error",
                "error_type": "VALIDATION_ERROR",
                "message": f"Target outcome '{target_outcome_id}' does not exist in OutcomeContract",
            }

        # 3. Validate proposed steps
        if not proposed_steps or not isinstance(proposed_steps, list):
            return {
                "status": "error",
                "error_type": "VALIDATION_ERROR",
                "message": "proposed_steps must be a non-empty list",
            }

        known_tools = {
            "verify_identity", "validate_documents", "run_risk_check",
            "setup_billing", "activate_account", "send_welcome_package",
            "verify_outcome", "check_service_status", "list_available_billing_providers",
        }

        parsed_steps: list[RecoveryStep] = []
        for step in proposed_steps:
            if not isinstance(step, dict):
                return {"status": "error", "error_type": "VALIDATION_ERROR", "message": "Each proposed step must be a dictionary"}
            tool_name = step.get("tool_name")
            if not tool_name or tool_name not in known_tools:
                return {"status": "error", "error_type": "VALIDATION_ERROR", "message": f"Unknown capability/tool '{tool_name}' in proposed steps"}

            tool_args = step.get("tool_args", {})
            if not isinstance(tool_args, dict):
                return {"status": "error", "error_type": "VALIDATION_ERROR", "message": f"Step for '{tool_name}' must have tool_args dictionary"}

            parsed_steps.append(
                RecoveryStep(
                    tool_name=tool_name,
                    tool_args=tool_args,
                    rationale=step.get("rationale", ""),
                    expected_result=step.get("expected_result", {}),
                    depends_on=step.get("depends_on", []),
                )
            )

        # 4. Validate evidence references if provided
        all_evidence = await self._store.get_all_evidence(workflow_id)
        existing_evidence_ids = {e.get("evidence_id") for e in all_evidence}
        if evidence_ids:
            for eid in evidence_ids:
                if eid not in existing_evidence_ids:
                    return {
                        "status": "error",
                        "error_type": "VALIDATION_ERROR",
                        "message": f"Referenced evidence '{eid}' does not exist in workflow evidence store",
                    }

        # 5. Check ordering constraints and prohibited outcomes
        completed_steps = await self._store.get_steps(workflow_id)
        completed_tools = {s.get("tool_name") for s in completed_steps if s.get("status") == "COMPLETED"}
        proposed_tool_names = [s.tool_name for s in parsed_steps]
        for c in contract.get("constraints", []):
            if c.get("constraint_id") == "identity_first":
                if "verify_identity" not in completed_tools and "verify_identity" not in proposed_tool_names:
                    if any(t in ("setup_billing", "activate_account") for t in proposed_tool_names):
                        return {
                            "status": "error",
                            "error_type": "VALIDATION_ERROR",
                            "message": "Proposed plan violates constraint: identity_first",
                        }

        prohibited = contract.get("prohibited_outcomes", [])
        if "account_activated_without_billing" in prohibited:
            if "setup_billing" not in completed_tools and "setup_billing" not in proposed_tool_names:
                if "activate_account" in proposed_tool_names:
                    return {
                        "status": "error",
                        "error_type": "VALIDATION_ERROR",
                        "message": "Proposed plan violates prohibited outcome: account_activated_without_billing",
                    }

        # 6. Supersede prior active recovery plans for this target outcome
        prior_plans = await self._store.get_recovery_plans(workflow_id)
        for p in prior_plans:
            if p.get("target_outcome_id") == target_outcome_id and p.get("status") in (
                RecoveryPlanStatus.PROPOSED.value,
                RecoveryPlanStatus.APPROVED.value,
                RecoveryPlanStatus.EXECUTING.value,
            ):
                p["status"] = RecoveryPlanStatus.SUPERSEDED.value
                await self._store.save_recovery_plan(workflow_id, p)

        # 7. Construct and persist RecoveryPlan
        confidence_enum = Confidence.MEDIUM
        try:
            confidence_enum = Confidence(confidence.upper())
        except Exception:
            confidence_enum = Confidence.MEDIUM

        plan = RecoveryPlan(
            workflow_id=workflow_id,
            target_outcome_id=target_outcome_id,
            failure_id=failure_id,
            diagnosis=diagnosis,
            root_cause=root_cause,
            objective=objective or f"Recover outcome: {target_outcome_id}",
            constraints=constraints or [],
            proposed_steps=parsed_steps,
            evidence_ids=evidence_ids or [],
            expected_evidence=expected_evidence or {},
            verification_strategy=verification_strategy or f"Independent verification of {target_outcome_id}",
            risks=risks or [],
            reasoning=reasoning,
            confidence=confidence_enum,
            rollback_notes=rollback_notes,
            status=RecoveryPlanStatus.PROPOSED,
        )

        await self._store.save_recovery_plan(workflow_id, plan.model_dump(mode="json"))

        # 8. Record event
        await self._engine._record_event(
            workflow_id=workflow_id,
            event_type=EventType.RECOVERY_PLAN_PROPOSED,
            title=f"Recovery Plan Proposed: {target_outcome_id}",
            detail=diagnosis,
            payload={
                "plan_id": plan.plan_id,
                "target_outcome_id": target_outcome_id,
                "proposed_steps": [s.model_dump(mode="json") for s in parsed_steps],
                "confidence": confidence_enum.value,
            },
            actor="recovery_specialist",
        )

        return {
            "status": "success",
            "plan_id": plan.plan_id,
            "target_outcome_id": target_outcome_id,
            "plan_status": RecoveryPlanStatus.PROPOSED.value,
            "proposed_steps_count": len(parsed_steps),
            "message": "Recovery plan submitted and persisted",
        }

    # ------------------------------------------------------------------
    # Evidence Recording (internal helper)
    # ------------------------------------------------------------------

    async def _record_evidence(
        self,
        workflow_id: str,
        source: str,
        evidence_type: EvidenceType,
        data: dict[str, Any],
    ) -> str:
        """Record a piece of evidence in the workflow's evidence store."""
        evidence = Evidence(
            workflow_id=workflow_id,
            source=source,
            evidence_type=evidence_type,
            data=data,
        )
        await self._store.save_evidence(
            workflow_id, evidence.model_dump(mode="json")
        )
        return evidence.evidence_id
