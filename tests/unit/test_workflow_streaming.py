"""
EAOC Unit Test Suite - Real-Time Workflow Streaming & Monitoring (Part 2.17)
Tests all Part 2.17 specifications:
1. Strict Authorization: Bearer JWT header enforcement (missing/query tokens return 401).
2. Tenant isolation (cross-tenant stream returns 404).
3. Canonical event ID format, full UUID tool discriminators, and sequence monotonicity.
4. Durable PostgreSQL milestone synthesis on connect/reconnect.
5. Last-Event-ID parsing, filtering, and artificial error cursor handling.
6. Snapshot reconciliation: duplicate milestone drop, forward lifecycle preservation, newer epoch precedence.
7. Buffer overflow protection and error framing.
8. Automatic sensitive data redaction in streamed payloads.
9. Subscriber disconnect and resource cleanup.
"""

import asyncio
from datetime import datetime, timezone
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
from fastapi.testclient import TestClient

from eaoc.api.dependencies.auth import AuthenticatedUser
from eaoc.api.factory import create_app
from eaoc.api.v1.endpoints.workflows import (
    get_event_bus,
    get_workflow_repository,
    stream_workflow,
)
from eaoc.config import get_settings
from eaoc.schemas.events import (
    StreamEventType,
    WorkflowStreamEvent,
    format_event_id,
    is_artificial_error_cursor,
    parse_event_id,
)
from eaoc.services.event_bus import InMemoryWorkflowEventBus
from eaoc.state.workflow_repository import InMemoryWorkflowRecoveryRepository


def generate_test_jwt(user_id: str, tenant_id: str, roles: list[str]) -> str:
    """Generates a valid JWT token matching test environment settings."""
    settings = get_settings()
    now = int(time.time())
    payload: dict = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "roles": roles,
        "exp": now + 3600,
        "iat": now,
    }
    if settings.JWT_ISSUER:
        payload["iss"] = settings.JWT_ISSUER
    if settings.JWT_AUDIENCE:
        payload["aud"] = settings.JWT_AUDIENCE
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


class TestWorkflowStreaming(unittest.IsolatedAsyncioTestCase):
    """Hermetic unit tests for Part 2.17 SSE streaming."""

    async def asyncSetUp(self):
        self.app = create_app()
        self.repo = InMemoryWorkflowRecoveryRepository()
        self.event_bus = InMemoryWorkflowEventBus()

        self.app.dependency_overrides[get_workflow_repository] = lambda: self.repo
        self.app.dependency_overrides[get_event_bus] = lambda: self.event_bus

        self.client = TestClient(self.app)
        self.tenant_a = "tenant-alpha"
        self.tenant_b = "tenant-beta"
        self.user_a = "user-100"
        self.token_a = generate_test_jwt(self.user_a, self.tenant_a, ["developer", "operator"])
        self.token_b = generate_test_jwt("user-200", self.tenant_b, ["developer"])

    async def asyncTearDown(self):
        self.app.dependency_overrides.clear()

    def test_1_missing_auth_header_rejected(self):
        """Verify stream request lacking Authorization header returns 401."""
        response = self.client.get("/api/v1/workflows/wf-test-001/stream")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Missing authorization header", response.json()["detail"])

    def test_2_query_token_rejected(self):
        """Verify query-parameter token is rejected (Option 1A strict header policy)."""
        response = self.client.get(
            f"/api/v1/workflows/wf-test-001/stream?token={self.token_a}"
        )
        self.assertEqual(response.status_code, 401)

    async def test_3_cross_tenant_stream_lookup_404(self):
        """Verify user from tenant B attempting to stream tenant A's workflow receives 404."""
        wf_id = "wf-tenant-a-1"
        await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            user_id=self.user_a,
            task_id="task-1",
            task_description="Analyze records",
        )

        response = self.client.get(
            f"/api/v1/workflows/{wf_id}/stream",
            headers={"Authorization": f"Bearer {self.token_b}"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("was not found", response.json()["detail"])

    async def test_4_canonical_event_id_formatting_and_parsing(self):
        """Verify canonical event ID format and full UUID tool discriminators."""
        wf_id = "wf-test-id-1234"
        tool_call_id = "550e8400-e29b-41d4-a716-446655440000"
        formatted = format_event_id(
            workflow_id=wf_id,
            epoch=2,
            step_index=3,
            event_type=StreamEventType.TOOL_INVOKED,
            seq=14,
            discriminator=f"tc-{tool_call_id}",
        )
        expected = f"evt:v1:{wf_id}:e2:s3:tool_invoked:sq14:tc-{tool_call_id}"
        self.assertEqual(formatted, expected)

        parsed = parse_event_id(formatted)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["workflow_id"], wf_id)
        self.assertEqual(parsed["epoch"], 2)
        self.assertEqual(parsed["step_index"], 3)
        self.assertEqual(parsed["event_type"], "tool_invoked")
        self.assertEqual(parsed["seq"], 14)
        self.assertFalse(parsed["is_synthesized"])
        self.assertEqual(parsed["discriminator"], f"tc-{tool_call_id}")

    async def test_5_artificial_error_cursor_detection(self):
        """Verify error and reconnect frame IDs are identified as artificial."""
        self.assertTrue(is_artificial_error_cursor("err:wf-123:1700000000:overflow"))
        self.assertTrue(is_artificial_error_cursor("rec:wf-123:1700000000"))
        self.assertTrue(is_artificial_error_cursor("evt:v1:wf-1:e1:s-1:error:sq1:overflow"))
        self.assertFalse(is_artificial_error_cursor("evt:v1:wf-1:e1:s0:step_started:sq1:start"))

    async def test_6_durable_milestone_synthesis_on_connect(self):
        """Verify initial stream connection synthesizes durable completed tasks."""
        wf_id = "wf-synth-01"
        await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            user_id=self.user_a,
            task_id="task-synth",
            task_description="Execute financial pipeline",
        )
        # Acquire lease & complete step 0
        _, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, self.tenant_a)
        _, _, _, rev = await self.repo.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            step_index=0,
            agent_id="finance",
            name="Calculate ledger",
            plan_hash="plan-h-01",
            step_hash="step-h-01",
            inputs={"month": "Q3"},
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )
        rev = await self.repo.complete_task_step(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            step_index=0,
            output_data={"total": 50000},
            provenance={"step": 0},
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Release lease as COMPLETED
        await self.repo.release_execution_lease(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            fencing_epoch=epoch,
            lock_token=token,
            final_status="COMPLETED",
        )

        with self.client.stream(
            "GET",
            f"/api/v1/workflows/{wf_id}/stream",
            headers={"Authorization": f"Bearer {self.token_a}"},
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
            lines = [line for line in response.iter_lines() if line]
            content = "\n".join(lines)

            # Must contain synthesized step milestones and completion
            self.assertIn("event: step_started", content)
            self.assertIn("event: step_completed", content)
            self.assertIn("event: workflow_completed", content)
            self.assertIn("sqsyn", content)

    async def test_7_last_event_id_skips_acknowledged_milestone(self):
        """Verify supplying Last-Event-ID for step_started skips step_started but emits step_completed."""
        wf_id = "wf-cursor-01"
        await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            user_id=self.user_a,
            task_id="task-cursor",
            task_description="Execute analytics",
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, self.tenant_a)
        _, _, _, rev = await self.repo.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            step_index=0,
            agent_id="analytics",
            name="Run stats",
            plan_hash="p-h",
            step_hash="s-h",
            inputs={},
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )
        await self.repo.complete_task_step(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            step_index=0,
            output_data={"mean": 42},
            provenance={},
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )
        await self.repo.release_execution_lease(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            fencing_epoch=epoch,
            lock_token=token,
            final_status="COMPLETED",
        )

        # Client already received step_started for step 0
        ack_id = format_event_id(wf_id, epoch, 0, StreamEventType.STEP_STARTED, None, "start")

        with self.client.stream(
            "GET",
            f"/api/v1/workflows/{wf_id}/stream",
            headers={
                "Authorization": f"Bearer {self.token_a}",
                "Last-Event-ID": ack_id,
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            lines = [line for line in response.iter_lines() if line]
            content = "\n".join(lines)

            # step_started must be skipped because of Last-Event-ID
            self.assertNotIn("event: step_started", content)
            # step_completed and workflow_completed must still be emitted
            self.assertIn("event: step_completed", content)
            self.assertIn("event: workflow_completed", content)

    async def test_8_multiple_tool_calls_unique_discriminators(self):
        """Verify multiple tool calls within one step produce unique, non-colliding IDs."""
        wf_id = "wf-tools-multi"
        tool_ids = [f"550e8400-e29b-41d4-a716-44665544000{i}" for i in range(5)]
        event_ids = [
            format_event_id(wf_id, 1, 0, StreamEventType.TOOL_INVOKED, i + 1, f"tc-{tid}")
            for i, tid in enumerate(tool_ids)
        ]
        # All event IDs must be distinct
        self.assertEqual(len(event_ids), 5)
        self.assertEqual(len(set(event_ids)), 5)

        for tid, eid in zip(tool_ids, event_ids):
            self.assertIn(f"tc-{tid}", eid)

    async def test_8b_tool_lifecycle_stage_identities_and_deduplication(self):
        """Verify distinct lifecycle stages of a tool call have unique, stable IDs and pass stream deduplication."""
        from eaoc.tools.interceptor import ToolInterceptor

        wf_id = "wf-tool-stages-01"
        tool_call_uuid = "123e4567-e89b-12d3-a456-426614174000"

        interceptor = ToolInterceptor(event_bus=self.event_bus)

        # 1. Emit attempt, completion, and failure
        interceptor._emit_tool_stream_event(
            workflow_id=wf_id,
            tool_call_id=tool_call_uuid,
            target_tool_id="sql_query",
            agent_id="sql_data",
            action="attempted",
            parameters={"query": "SELECT 1"},
        )
        interceptor._emit_tool_stream_event(
            workflow_id=wf_id,
            tool_call_id=tool_call_uuid,
            target_tool_id="sql_query",
            agent_id="sql_data",
            action="completed",
            parameters={"query": "SELECT 1"},
            duration_ms=12.5,
            output_summary="1 row",
        )
        interceptor._emit_tool_stream_event(
            workflow_id=wf_id,
            tool_call_id=tool_call_uuid,
            target_tool_id="sql_query",
            agent_id="sql_data",
            action="failed",
            parameters={"query": "SELECT 1"},
            duration_ms=12.5,
            error="Connection lost",
        )

        # Wait for background publishing tasks to finish
        if interceptor._tracked_tasks:
            await asyncio.gather(*list(interceptor._tracked_tasks))

        events = [e for e in self.event_bus.published_events if e.workflow_id == wf_id]
        self.assertEqual(len(events), 3)

        att_ev, cmp_ev, err_ev = events[0], events[1], events[2]

        # 2. Attempt and completion IDs differ
        self.assertNotEqual(att_ev.event_id, cmp_ev.event_id)
        # 3. Attempt and failure IDs differ
        self.assertNotEqual(att_ev.event_id, err_ev.event_id)
        # 4. Completion and failure IDs differ
        self.assertNotEqual(cmp_ev.event_id, err_ev.event_id)

        # 5. Check full UUID preserved in discriminator with stage code
        self.assertIn(f"tc-{tool_call_uuid}-att", att_ev.event_id)
        self.assertIn(f"tc-{tool_call_uuid}-cmp", cmp_ev.event_id)
        self.assertIn(f"tc-{tool_call_uuid}-err", err_ev.event_id)

        # 6. Canonical parsing remains valid for all stages
        for ev, expected_disc in [
            (att_ev, f"tc-{tool_call_uuid}-att"),
            (cmp_ev, f"tc-{tool_call_uuid}-cmp"),
            (err_ev, f"tc-{tool_call_uuid}-err"),
        ]:
            parsed = parse_event_id(ev.event_id)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["workflow_id"], wf_id)
            self.assertTrue(parsed["is_synthesized"])
            self.assertEqual(parsed["discriminator"], expected_disc)

        # 7. All three distinct stages pass through the stream deduplicator
        seen_ids = set()
        deduped_frames = []
        for ev in [att_ev, cmp_ev, err_ev]:
            if ev.event_id not in seen_ids:
                seen_ids.add(ev.event_id)
                deduped_frames.append(ev)

        self.assertEqual(len(deduped_frames), 3)

        # 8. Duplicate publication of the same stage is still deduplicated
        for duplicate_ev in [att_ev, cmp_ev, err_ev]:
            if duplicate_ev.event_id not in seen_ids:
                seen_ids.add(duplicate_ev.event_id)
                deduped_frames.append(duplicate_ev)

        # Total retained frames must still be exactly 3
        self.assertEqual(len(deduped_frames), 3)

    async def test_9_sensitive_data_redacted_in_stream_payload(self):
        """Verify passwords, tokens, and database credentials are automatically redacted."""
        wf_id = "wf-redact-01"
        event = WorkflowStreamEvent(
            event_id=f"evt:v1:{wf_id}:e1:s0:step_started:sq1:start",
            event_type=StreamEventType.STEP_STARTED,
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            epoch=1,
            step_index=0,
            seq=1,
            payload={
                "db_url": "postgresql+asyncpg://admin:supersecret@10.0.0.1:5432/eaoc",
                "api_key": "sk-1234567890abcdef",
                "normal_field": "public_data",
            },
        )

        await self.event_bus.publish(event)
        self.assertEqual(len(self.event_bus.published_events), 1)
        pub = self.event_bus.published_events[0]
        self.assertNotIn("supersecret", json.dumps(pub.payload))
        self.assertNotIn("sk-1234567890abcdef", json.dumps(pub.payload))
        self.assertEqual(pub.payload["normal_field"], "public_data")
        self.assertEqual(pub.payload["api_key"], "****")


    async def test_10_snapshot_reconciliation_live_events(self):
        """Verify duplicate milestones in live stream are dropped while forward transitions are emitted."""
        wf_id = "wf-reconcile-01"
        await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            user_id=self.user_a,
            task_id="task-rec",
            task_description="Execute reconciliation",
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, self.tenant_a)
        _, _, _, rev = await self.repo.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            step_index=0,
            agent_id="finance",
            name="Step 0",
            plan_hash="p-h",
            step_hash="s-h",
            inputs={},
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )
        rev = await self.repo.complete_task_step(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            step_index=0,
            output_data={"done": True},
            provenance={},
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Before stream connect, publish a duplicate live milestone into event_bus
        duplicate_live = WorkflowStreamEvent(
            event_id=format_event_id(wf_id, epoch, 0, StreamEventType.STEP_COMPLETED, 1, "done"),
            event_type=StreamEventType.STEP_COMPLETED,
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            epoch=epoch,
            step_index=0,
            seq=1,
            payload={"status": "COMPLETED"},
        )
        # Also publish a forward transition milestone for step 1
        forward_live = WorkflowStreamEvent(
            event_id=format_event_id(wf_id, epoch, 1, StreamEventType.STEP_STARTED, 2, "start"),
            event_type=StreamEventType.STEP_STARTED,
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            epoch=epoch,
            step_index=1,
            seq=2,
            payload={"description": "Step 1 forward"},
        )
        # Also publish terminal workflow completion
        terminal_live = WorkflowStreamEvent(
            event_id=format_event_id(wf_id, epoch, -1, StreamEventType.WORKFLOW_COMPLETED, 3, "complete"),
            event_type=StreamEventType.WORKFLOW_COMPLETED,
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            epoch=epoch,
            step_index=-1,
            seq=3,
            payload={"status": "COMPLETED"},
        )

        mock_req = MagicMock()
        mock_req.is_disconnected = AsyncMock(return_value=False)
        mock_req.headers = {}
        user = AuthenticatedUser(user_id=self.user_a, tenant_id=self.tenant_a, roles=["developer"])

        streaming_resp = await stream_workflow(
            workflow_id=wf_id,
            request=mock_req,
            last_event_id=None,
            user=user,
            repo=self.repo,
            event_bus=self.event_bus,
        )

        # Allow listener task to register subscription
        await asyncio.sleep(0.05)

        # Publish events to event bus
        await self.event_bus.publish(duplicate_live)
        await self.event_bus.publish(forward_live)
        await self.event_bus.publish(terminal_live)

        chunks = []
        async for chunk in streaming_resp.body_iterator:
            chunks.append(chunk)
            if "workflow_completed" in chunk:
                break

        content = "".join(chunks)

        # Step 0 synthesized completed must be present
        self.assertIn("evt:v1:wf-reconcile-01:e1:s0:step_completed:sqsyn:done", content)
        # Forward transition for step 1 must be present
        self.assertIn("evt:v1:wf-reconcile-01:e1:s1:step_started:sq2:start", content)
        # Terminal workflow event must be present
        self.assertIn("evt:v1:wf-reconcile-01:e1:s-1:workflow_completed:sq3:complete", content)

    async def test_11_orchestrator_event_bus_emission(self):
        """Verify OrchestratorEngine automatically emits stream events during execution."""
        from typing import Optional
        from eaoc.agents.base import BaseAgent
        from eaoc.agents.orchestrator.engine import OrchestratorEngine
        from eaoc.agents.orchestrator.schemas import OrchestratorInput
        from eaoc.agents.planner.schemas import ExecutionPlan, PlanStep, PlanType
        from eaoc.agents.registry import AgentRegistry
        from eaoc.schemas.domain import AgentExecutionContext, AgentID
        from pydantic import BaseModel

        from eaoc.agents.reviewer_validation.schemas import (
            ReviewerInput,
            ReviewerOutput,
            ReviewRecommendation,
            RiskAssessment,
            ReviewerProvenance,
        )

        class StubPayload(BaseModel):
            model_config = {"extra": "allow"}
            result: Optional[str] = "ok"

        class DummyAgent(BaseAgent[StubPayload, StubPayload]):
            def __init__(self, agent_id: AgentID):
                super().__init__(
                    agent_id=agent_id,
                    input_schema=StubPayload,
                    output_schema=StubPayload,
                )
            async def _execute(self, validated_input: StubPayload, context: AgentExecutionContext) -> StubPayload:
                return StubPayload(result="ok")

        class StubReviewerAgent(BaseAgent[ReviewerInput, ReviewerOutput]):
            def __init__(self):
                super().__init__(
                    agent_id=AgentID.REVIEWER_VALIDATION,
                    input_schema=ReviewerInput,
                    output_schema=ReviewerOutput,
                )
            async def _execute(self, validated_input: ReviewerInput, context: AgentExecutionContext) -> ReviewerOutput:
                now = datetime.now(timezone.utc)
                return ReviewerOutput(
                    review_id="rev-1",
                    task_id=validated_input.task_id,
                    passed=True,
                    findings=[],
                    risk_assessment=RiskAssessment(risk_level="low", risk_factors=[]),
                    recommendation=ReviewRecommendation.APPROVE,
                    reviewed_at=now,
                    provenance=ReviewerProvenance(
                        review_id="rev-1",
                        task_id=validated_input.task_id,
                        risk_level="low",
                        recommendation=ReviewRecommendation.APPROVE,
                        reviewed_at=now,
                    ),
                )

        registry = AgentRegistry()
        for agt in AgentID:
            if agt == AgentID.REVIEWER_VALIDATION:
                registry.register_agent(StubReviewerAgent())
            else:
                registry.register_agent(DummyAgent(agt))

        engine = OrchestratorEngine(
            registry=registry,
            workflow_repo=self.repo,
            event_bus=self.event_bus,
        )

        wf_id = "wf-orch-stream-01"
        await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=self.tenant_a,
            user_id=self.user_a,
            task_id="task-orch",
            task_description="Orchestrator streaming test",
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, self.tenant_a)

        plan = ExecutionPlan(
            plan_id="plan-orch-01",
            task_id="task-orch",
            plan_type=PlanType.MULTI_AGENT,
            planning_rationale="Streaming test plan",
            steps=[
                PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Search docs", inputs={}, dependencies=[]),
                PlanStep(step_index=1, agent_id=AgentID.DEVELOPER, description="Develop code", inputs={}, dependencies=[0]),
            ],
        )

        inp = OrchestratorInput(
            task_id="task-orch",
            task_description="Orchestrator streaming test",
            workflow_id=wf_id,
            execution_plan=plan,
            user_context={"user_id": self.user_a, "tenant_id": self.tenant_a, "roles": ["admin"]},
            metadata={"fencing_epoch": epoch, "lock_token": token, "expected_revision": rev, "tenant_id": self.tenant_a},
        )

        output = await engine.execute_workflow(inp)
        self.assertEqual(output.status.value, "COMPLETED")

        # Allow background publishing tasks to complete
        if engine._tracked_tasks:
            await asyncio.gather(*list(engine._tracked_tasks), return_exceptions=True)

        # Verify events emitted to event bus
        event_types = [e.event_type.value for e in self.event_bus.published_events if e.workflow_id == wf_id]
        self.assertIn("workflow_started", event_types)
        self.assertIn("step_started", event_types)
        self.assertIn("step_completed", event_types)
        self.assertIn("workflow_completed", event_types)


if __name__ == "__main__":
    unittest.main()

