"""
EAOC LangGraph Assembly & Lifecycle Routing (Part 2.18.3.1)

Defines the isolated LangGraph assembly stage for EAOC stateful multi-agent
orchestration. Binds the 12 lifecycle operations implemented in OrchestrationNodes
into a deterministic StateGraph with explicit conditional edges.

PostgreSQL remains the canonical durable state store; no competing LangGraph
checkpointer is introduced. Fencing, lease management, replay safety, HITL approvals,
and bounded contextual re-planning are preserved.
"""

from typing import Any, Dict, Optional
from langgraph.graph import StateGraph, START, END
from langgraph.graph.state import CompiledStateGraph

from eaoc.orchestration.state import GraphWorkflowState
from eaoc.orchestration.nodes import OrchestrationNodes
from eaoc.schemas.domain import WorkflowStatus


# =============================================================================
# Explicit Lifecycle Router Functions
# =============================================================================

def route_init_and_recover(state: GraphWorkflowState) -> str:
    """
    Routes from init_and_recover:
    - 'fail_workflow': if unrecoverable initialization error or status FAILED
    - 'plan_resolution': default progression to plan validation/generation
    """
    if state.get("next_route") == "fail_workflow" or state.get("status") == WorkflowStatus.FAILED.value:
        return "fail_workflow"
    return "plan_resolution"


def route_plan_resolution(state: GraphWorkflowState) -> str:
    """
    Routes from plan_resolution:
    - 'fail_workflow': if plan validation fails or PlannerAgent fails
    - 'prepare_step': plan validated and bound to plan_hash; ready to prepare step 0
    """
    if state.get("next_route") == "fail_workflow" or state.get("status") == WorkflowStatus.FAILED.value:
        return "fail_workflow"
    return "prepare_step"


def route_prepare_step(state: GraphWorkflowState) -> str:
    """
    Routes from prepare_step:
    - 'reviewer_gate': when all plan steps have completed (step_cursor >= len(steps))
    - 'prepare_step': when step was fast-forwarded from repository (check_more_steps)
    - 'fail_workflow': on replay safety, ambiguous side effect, dependency, or fencing failure
    - 'evaluate_risk_gate': step prepared and cleared for risk gate evaluation
    """
    route = state.get("next_route")
    if route == "reviewer_gate":
        return "reviewer_gate"
    if route == "check_more_steps":
        return "prepare_step"
    if route == "fail_workflow" or state.get("status") == WorkflowStatus.FAILED.value:
        return "fail_workflow"
    return "evaluate_risk_gate"


def route_evaluate_risk_gate(state: GraphWorkflowState) -> str:
    """
    Routes from evaluate_risk_gate:
    - 'pause_for_approval': risk gate requires human approval and no authorized decision exists
    - 'fail_workflow': human approval was explicitly rejected or binding validation failed
    - 'dispatch_step': low/medium risk or valid approved continuation verified
    """
    route = state.get("next_route")
    if route == "pause_for_approval" or state.get("status") == WorkflowStatus.WAITING_APPROVAL.value:
        return "pause_for_approval"
    if route == "fail_workflow" or state.get("status") == WorkflowStatus.FAILED.value:
        return "fail_workflow"
    return "dispatch_step"


def route_pause_for_approval(state: GraphWorkflowState) -> str:
    """
    Routes from pause_for_approval:
    - 'END': workflow paused in WAITING_APPROVAL status; lease released in PostgreSQL
    - 'fail_workflow': database persistence failed
    """
    route = state.get("next_route")
    if route == "fail_workflow" or state.get("status") == WorkflowStatus.FAILED.value:
        return "fail_workflow"
    return "END"


def route_dispatch_step(state: GraphWorkflowState) -> str:
    """
    Routes from dispatch_step:
    - 'record_step_success': agent completed task step successfully
    - 'handle_step_failure': agent execution failed, timed out, or RBAC denied
    """
    if state.get("last_dispatch_success") or state.get("next_route") == "record_step_success":
        return "record_step_success"
    return "handle_step_failure"


def route_record_step_success(state: GraphWorkflowState) -> str:
    """
    Routes from record_step_success:
    - 'fail_workflow': database persistence failed
    - 'prepare_step': step completion and provenance committed; prepare next step
    """
    if state.get("next_route") == "fail_workflow" or state.get("status") == WorkflowStatus.FAILED.value:
        return "fail_workflow"
    return "prepare_step"


def route_handle_step_failure(state: GraphWorkflowState) -> str:
    """
    Routes from handle_step_failure:
    - 'prepare_step': optional step failed and was skipped (check_more_steps)
    - 'contextual_replan': non-prohibited failure within max replan budget
    - 'fail_workflow': prohibited replan failure or max replan attempts exhausted
    """
    route = state.get("next_route")
    if route == "check_more_steps":
        return "prepare_step"
    if route == "contextual_replan":
        return "contextual_replan"
    return "fail_workflow"


def route_contextual_replan(state: GraphWorkflowState) -> str:
    """
    Routes from contextual_replan:
    - 'prepare_step': revised plan generated; advancing to prepare first revised step
    - 'fail_workflow': contextual replanning failed or rejected
    """
    if state.get("next_route") == "fail_workflow" or state.get("status") == WorkflowStatus.FAILED.value:
        return "fail_workflow"
    return "prepare_step"


def route_reviewer_gate(state: GraphWorkflowState) -> str:
    """
    Routes from reviewer_gate:
    - 'finalize_workflow': reviewer recommendation is APPROVE
    - 'pause_for_approval': reviewer recommendation is ESCALATE (HITL required)
    - 'fail_workflow': reviewer recommendation is REJECT
    """
    route = state.get("next_route")
    if route == "pause_for_approval":
        return "pause_for_approval"
    if route == "fail_workflow" or state.get("status") == WorkflowStatus.FAILED.value:
        return "fail_workflow"
    return "finalize_workflow"


# =============================================================================
# Graph Assembly & Compilation
# =============================================================================

def build_workflow_graph(nodes: OrchestrationNodes) -> StateGraph:
    """
    Assembles the uncompiled StateGraph binding OrchestrationNodes lifecycle operations.

    Node Topology:
    1.  init_and_recover    - Rehydrates task state, fences epoch, validates credentials
    2.  plan_resolution     - Validates supplied plan or dispatches PlannerAgent
    3.  prepare_step        - Fast-forwards completed steps, checks dependencies, enforces replay safety
    4.  evaluate_risk_gate  - Section 9.2 risk classification and approval decision matching
    5.  pause_for_approval  - Persists ApprovalRequest in PostgreSQL, releases lease, enters WAITING_APPROVAL
    6.  dispatch_step       - Enforces RBAC, assembles context, dispatches agent via AgentRegistry with retries
    7.  record_step_success - Persists task completion and provenance in PostgreSQL, emits STEP_COMPLETED
    8.  handle_step_failure - Skips optional steps, filters prohibited replan classes, routes to replan/fail
    9.  contextual_replan   - Dispatches contextual re-planning to PlannerAgent within budget
    10. reviewer_gate       - Executes ReviewerValidation quality gate across aggregated outputs
    11. finalize_workflow   - Releases lease with COMPLETED status, constructs FinalResponse (terminal)
    12. fail_workflow       - Releases lease with FAILED status, records error info (terminal)
    """
    builder = StateGraph(GraphWorkflowState)

    # 1. Register Nodes
    builder.add_node("init_and_recover", nodes.node_init_and_recover)
    builder.add_node("plan_resolution", nodes.node_plan_resolution)
    builder.add_node("prepare_step", nodes.node_prepare_step)
    builder.add_node("evaluate_risk_gate", nodes.node_evaluate_risk_gate)
    builder.add_node("pause_for_approval", nodes.node_pause_for_approval)
    builder.add_node("dispatch_step", nodes.node_dispatch_step)
    builder.add_node("record_step_success", nodes.node_record_step_success)
    builder.add_node("handle_step_failure", nodes.node_handle_step_failure)
    builder.add_node("contextual_replan", nodes.node_contextual_replan)
    builder.add_node("reviewer_gate", nodes.node_reviewer_gate)
    builder.add_node("finalize_workflow", nodes.node_finalize_workflow)
    builder.add_node("fail_workflow", nodes.node_fail_workflow)

    # 2. Entry Point
    builder.add_edge(START, "init_and_recover")

    # 3. Conditional Edges
    builder.add_conditional_edges(
        "init_and_recover",
        route_init_and_recover,
        {
            "plan_resolution": "plan_resolution",
            "fail_workflow": "fail_workflow",
        },
    )

    builder.add_conditional_edges(
        "plan_resolution",
        route_plan_resolution,
        {
            "prepare_step": "prepare_step",
            "fail_workflow": "fail_workflow",
        },
    )

    builder.add_conditional_edges(
        "prepare_step",
        route_prepare_step,
        {
            "evaluate_risk_gate": "evaluate_risk_gate",
            "reviewer_gate": "reviewer_gate",
            "prepare_step": "prepare_step",
            "fail_workflow": "fail_workflow",
        },
    )

    builder.add_conditional_edges(
        "evaluate_risk_gate",
        route_evaluate_risk_gate,
        {
            "dispatch_step": "dispatch_step",
            "pause_for_approval": "pause_for_approval",
            "fail_workflow": "fail_workflow",
        },
    )

    builder.add_conditional_edges(
        "pause_for_approval",
        route_pause_for_approval,
        {
            "END": END,
            "fail_workflow": "fail_workflow",
        },
    )

    builder.add_conditional_edges(
        "dispatch_step",
        route_dispatch_step,
        {
            "record_step_success": "record_step_success",
            "handle_step_failure": "handle_step_failure",
        },
    )

    builder.add_conditional_edges(
        "record_step_success",
        route_record_step_success,
        {
            "prepare_step": "prepare_step",
            "fail_workflow": "fail_workflow",
        },
    )

    builder.add_conditional_edges(
        "handle_step_failure",
        route_handle_step_failure,
        {
            "prepare_step": "prepare_step",
            "contextual_replan": "contextual_replan",
            "fail_workflow": "fail_workflow",
        },
    )

    builder.add_conditional_edges(
        "contextual_replan",
        route_contextual_replan,
        {
            "prepare_step": "prepare_step",
            "fail_workflow": "fail_workflow",
        },
    )

    builder.add_conditional_edges(
        "reviewer_gate",
        route_reviewer_gate,
        {
            "finalize_workflow": "finalize_workflow",
            "pause_for_approval": "pause_for_approval",
            "fail_workflow": "fail_workflow",
        },
    )

    # 4. Terminal Direct Edges (Strictly no cycles back into graph)
    builder.add_edge("finalize_workflow", END)
    builder.add_edge("fail_workflow", END)

    return builder


def compile_workflow_graph(nodes: OrchestrationNodes) -> CompiledStateGraph:
    """
    Compiles the EAOC StateGraph without a competing LangGraph checkpointer.
    Canonical durable state resides strictly in Supabase PostgreSQL via the injected repositories.
    """
    builder = build_workflow_graph(nodes)
    return builder.compile()
