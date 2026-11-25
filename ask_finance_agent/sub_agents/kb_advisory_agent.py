"""Sub-Agent 2: Procurement & Coupa Knowledge Base Advisory Specialist (`gemini-2.5-flash`)."""

from __future__ import annotations

from google.adk.agents import LlmAgent

from ask_finance_agent.callbacks import (
    after_model_callback,
    after_tool_callback,
    before_model_callback,
    before_tool_callback,
)
from ask_finance_agent.config.settings import get_settings
from ask_finance_agent.prompts import KB_ADVISORY_SUBAGENT_PROMPT
from ask_finance_agent.tools import (
    evaluate_supplier_exemption_status,
    lookup_coupa_oracle_invoice_record,
    open_interactive_coupa_kb_canvas,
)


def build_kb_advisory_subagent() -> LlmAgent:
    """Constructs the Coupa KB Canvas, PO amendment, SIM, and Expense Portal advisory specialist."""
    settings = get_settings()
    return LlmAgent(
        name="procurement_kb_advisory_agent",
        model=settings.gemini_flash_model,
        description=(
            "Coupa how-to navigation, Non-PO coding (KB0010101), 3-way match goods "
            "receipting (KB0010106), delegation & custom views (KB0010107), Enterprise IAM access, "
            "PO amendments, Coupa SIM, and interactive Side-Panel KB Canvas specialist."
        ),
        instruction=KB_ADVISORY_SUBAGENT_PROMPT,
        tools=[
            open_interactive_coupa_kb_canvas,
            evaluate_supplier_exemption_status,
            lookup_coupa_oracle_invoice_record,
        ],
        before_model_callback=before_model_callback,
        after_model_callback=after_model_callback,
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
    )


procurement_kb_advisory_agent = build_kb_advisory_subagent()
