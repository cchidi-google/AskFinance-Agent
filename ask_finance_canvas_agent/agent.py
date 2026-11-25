"""Root Coordinator & Sub-Agents for `ask_finance_canvas_agent` (App-in-Canvas).

Leaves the primary `ask_finance_agent` (`ask_finance_orchestrator`) untouched for
clean side-by-side A/B evaluation in Gemini Enterprise while wiring all tools
and specialist sub-agents to `AppInCanvasPlugin` (`autoFullscreen=True`).
"""

from __future__ import annotations

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.apps import App

from ask_finance_agent.config.settings import get_settings
from ask_finance_agent.observability.tracing import configure_opentelemetry_tracing
from ask_finance_agent.plugins import GbsPolicyGuardrailPlugin
from ask_finance_agent.prompts import (
    AGENT_CONSTITUTION_PROMPT,
    DIAGNOSTIC_SUBAGENT_PROMPT,
    ESCALATION_HITL_SUBAGENT_PROMPT,
    KB_ADVISORY_SUBAGENT_PROMPT,
)
from ask_finance_agent.tools import (
    evaluate_supplier_exemption_status,
    lookup_coupa_oracle_invoice_record,
    open_interactive_coupa_kb_canvas,
    open_urgent_payment_acceleration_form,
    request_missing_invoice_identifier_form,
    stage_servicenow_case_intake_form,
    submit_confirmed_servicenow_case,
)

from .callbacks import (
    before_tool_callback,
    canvas_after_model_callback,
    canvas_after_tool_callback,
    canvas_before_model_callback,
)
from .canvas_workspace_plugin import AppInCanvasPlugin

configure_opentelemetry_tracing()

APP_IN_CANVAS_PROMPT_ADDENDUM = """

<APP_IN_CANVAS_MODE>
You are running in **App-in-Canvas Workspace Mode (`ask_finance_canvas_orchestrator`)**:
- Every single response automatically renders inside a persistent full-screen A2UI v0.9
  `Canvas` (`gbs-app-in-canvas`) containing your Agent Text Response Card first, any
  interactive form/table/policy cards below it, and the `AskFinance Agent` composer at
  the bottom.
- Keep your markdown narrative crisp, structured, and easy to scan inside the Canvas feed.
- **IMPORTANT — No `Cross-System Coupa & Oracle Tracker` Card**: The standalone
  `Cross-System Coupa & Oracle Tracker` card (`Surface D` / `gbs-invoice-status-*`) is NOT
  displayed in prompt response outputs. Present all Coupa approval details, Oracle voucher/EFT
  status, and Net 30 submission-date math directly and clearly in your markdown text response,
  and NEVER refer the user to a "Cross-System Coupa & Oracle Tracker" or "status tracker card".
- If the user launches the workspace without specifying an invoice, PO, supplier, or KB
  topic yet, welcome them concisely in 2-3 sentences without calling a tool.
</APP_IN_CANVAS_MODE>
"""


def build_canvas_diagnostic_subagent() -> LlmAgent:
    """Constructs the App-in-Canvas Invoice & Payment Diagnostic Specialist."""
    settings = get_settings()
    return LlmAgent(
        name="invoice_payment_diagnostic_agent",
        model=settings.gemini_flash_model,
        description=(
            "Fast Coupa + Oracle invoice status, Net 30 due date math (KB0010108), "
            "Quick-Lookup form, urgent early payment intake (PAY-01), and Supplier "
            "Email Exemptions (KB0010102) specialist for App-in-Canvas mode."
        ),
        instruction=DIAGNOSTIC_SUBAGENT_PROMPT + APP_IN_CANVAS_PROMPT_ADDENDUM,
        tools=[
            lookup_coupa_oracle_invoice_record,
            request_missing_invoice_identifier_form,
            evaluate_supplier_exemption_status,
            open_urgent_payment_acceleration_form,
        ],
        before_model_callback=canvas_before_model_callback,
        after_model_callback=canvas_after_model_callback,
        before_tool_callback=before_tool_callback,
        after_tool_callback=canvas_after_tool_callback,
    )


def build_canvas_kb_advisory_subagent() -> LlmAgent:
    """Constructs the App-in-Canvas Procurement & Coupa KB Advisory Specialist."""
    settings = get_settings()
    return LlmAgent(
        name="procurement_kb_advisory_agent",
        model=settings.gemini_flash_model,
        description=(
            "Coupa how-to navigation, Non-PO coding (KB0010101), 3-way match goods "
            "receipting (KB0010106), delegation & custom views (KB0010107), and "
            "interactive KB Canvas specialist for App-in-Canvas mode."
        ),
        instruction=KB_ADVISORY_SUBAGENT_PROMPT + APP_IN_CANVAS_PROMPT_ADDENDUM,
        tools=[
            open_interactive_coupa_kb_canvas,
            evaluate_supplier_exemption_status,
            lookup_coupa_oracle_invoice_record,
        ],
        before_model_callback=canvas_before_model_callback,
        after_model_callback=canvas_after_model_callback,
        before_tool_callback=before_tool_callback,
        after_tool_callback=canvas_after_tool_callback,
    )


def build_canvas_reconciliation_pipeline() -> SequentialAgent:
    """Builds the 2-step `SequentialAgent` that merges PAY-03 table+chart & P1 form in one Canvas."""
    settings = get_settings()

    reconcile_step = LlmAgent(
        name="statement_reconciliation_step_agent",
        model=settings.gemini_pro_model,
        description="Step 1: Reconciles multi-invoice supplier statements for the unified Canvas.",
        instruction=(
            "Step 1 of Sequential Escalation Pipeline: Call `lookup_coupa_oracle_invoice_record` "
            "to reconcile all listed statement invoices and queue the reconciliation table and chart. "
            "Store a concise summary of paid vs blocked invoices."
        ),
        tools=[lookup_coupa_oracle_invoice_record],
        output_key="statement_reconciliation_summary",
        before_model_callback=canvas_before_model_callback,
        after_model_callback=canvas_after_model_callback,
        before_tool_callback=before_tool_callback,
        after_tool_callback=canvas_after_tool_callback,
    )

    stage_ticket_step = LlmAgent(
        name="servicenow_p1_staging_step_agent",
        model=settings.gemini_pro_model,
        description="Step 2: Stages the mandatory P1 Senior AP Escalation ServiceNow form.",
        instruction=(
            "Step 2 of Sequential Escalation Pipeline: Using `{statement_reconciliation_summary}`, "
            "call `stage_servicenow_case_intake_form` with `service_area='LEGAL_ESCALATION'` and "
            "`priority='P1'`, then present a clear executive summary of all reconciled invoices "
            "and invite confirmation of the P1 ServiceNow escalation form."
        ),
        tools=[stage_servicenow_case_intake_form],
        before_model_callback=canvas_before_model_callback,
        after_model_callback=canvas_after_model_callback,
        before_tool_callback=before_tool_callback,
        after_tool_callback=canvas_after_tool_callback,
    )

    return SequentialAgent(
        name="reconciliation_to_escalation_pipeline",
        description=(
            "Deterministic 2-step SequentialAgent pipeline reconciling multi-invoice "
            "legal hold notices and staging a P1 ServiceNow case inside one unified Canvas."
        ),
        sub_agents=[reconcile_step, stage_ticket_step],
    )


def build_canvas_escalation_subagent() -> LlmAgent:
    """Constructs the App-in-Canvas Escalation, Security & HITL Specialist."""
    settings = get_settings()
    pipeline = build_canvas_reconciliation_pipeline()
    return LlmAgent(
        name="escalation_hitl_specialist_agent",
        model=settings.gemini_pro_model,
        description=(
            "Frontier reasoning specialist (`gemini-4-argon`) for multi-invoice legal reminders "
            "(PAY-03), external email domain mismatch security holds (PAY-05), approved "
            "wrong-amount Oracle holds (INV-11), and 2-step HITL ServiceNow cases in Canvas."
        ),
        instruction=ESCALATION_HITL_SUBAGENT_PROMPT + APP_IN_CANVAS_PROMPT_ADDENDUM,
        tools=[
            lookup_coupa_oracle_invoice_record,
            stage_servicenow_case_intake_form,
            submit_confirmed_servicenow_case,
            open_urgent_payment_acceleration_form,
        ],
        sub_agents=[pipeline],
        before_model_callback=canvas_before_model_callback,
        after_model_callback=canvas_after_model_callback,
        before_tool_callback=before_tool_callback,
        after_tool_callback=canvas_after_tool_callback,
    )


def build_canvas_orchestrator_agent() -> LlmAgent:
    """Constructs `ask_finance_canvas_orchestrator` (App-in-Canvas)."""
    settings = get_settings()
    return LlmAgent(
        name="ask_finance_canvas_orchestrator",
        model=settings.gemini_pro_model,
        description=(
            "AskFinance Agent — Full-screen interactive A2UI v0.9 Workspace "
            "where every turn renders inside a unified Canvas with toolbar navigation, "
            "live diagnostic tables/charts/forms, session transcript, and in-Canvas chat."
        ),
        instruction=AGENT_CONSTITUTION_PROMPT + APP_IN_CANVAS_PROMPT_ADDENDUM,
        tools=[
            lookup_coupa_oracle_invoice_record,
            request_missing_invoice_identifier_form,
            open_interactive_coupa_kb_canvas,
            evaluate_supplier_exemption_status,
            open_urgent_payment_acceleration_form,
            stage_servicenow_case_intake_form,
            submit_confirmed_servicenow_case,
        ],
        sub_agents=[
            build_canvas_diagnostic_subagent(),
            build_canvas_kb_advisory_subagent(),
            build_canvas_escalation_subagent(),
        ],
        before_model_callback=canvas_before_model_callback,
        after_model_callback=canvas_after_model_callback,
        before_tool_callback=before_tool_callback,
        after_tool_callback=canvas_after_tool_callback,
    )


root_agent = build_canvas_orchestrator_agent()

app = App(
    name="ask_finance_canvas_agent",
    root_agent=root_agent,
    plugins=[
        GbsPolicyGuardrailPlugin(),
        AppInCanvasPlugin(auto_fullscreen=True),
    ],
)
