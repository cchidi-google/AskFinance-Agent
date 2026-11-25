"""Lifecycle Callbacks for `ask_finance_canvas_agent` (App-in-Canvas).

1. On initial launch (`🎧 Launch AskFinance Agent` / `canvas_nav_home`), skips
   the LLM call and immediately opens the minimal `Canvas` showing ONLY `AskFinance Agent`.
2. On `toggle_canvas_fullscreen`, flips `state["canvas_auto_fullscreen"]` and immediately
   re-renders the active Canvas view in Full-Screen or Split-Screen without an LLM call.
3. Filters out Google ADK's synthetic `For context: ... <<<BEGIN_QUOTED_AGENT_CONTENT>>>`
   sub-agent handoff messages so internal transfer transcripts NEVER overwrite the user's
   inquiry or appear in the agent response.
4. Rewrites in-Canvas composer (`submit_canvas_chat_prompt`), follow-up chip
   (`select_followup_chip`), and form action events into clean natural-language queries.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from ask_finance_agent.callbacks import (
    after_tool_callback,
    before_tool_callback,
    parse_a2ui_user_action_event,
)
from ask_finance_agent.memory.async_worker import schedule_async_memory_consolidation
from ask_finance_agent.memory.compaction import get_compaction_manager
from ask_finance_agent.observability.intent_outcome_tracker import get_intent_outcome_tracker
from ask_finance_agent.observability.tracing import start_agent_span
from ask_finance_agent.orchestration.model_router import get_model_router
from ask_finance_agent.plugins.a2ui_canvas_plugin import build_genai_a2ui_parts

from .canvas_workspace_plugin import (
    APP_CANVAS_SURFACE_ID,
    HELPDESK_CARD_TITLE,
    attach_app_in_canvas_to_response,
    build_unified_app_in_canvas_surface,
    filter_prompt_display_surfaces,
    is_adk_internal_context_text,
    sanitize_display_text,
)

__all__ = [
    "after_tool_callback",
    "before_tool_callback",
    "canvas_after_model_callback",
    "canvas_after_tool_callback",
    "canvas_before_model_callback",
    "extract_genuine_user_text",
    "is_initial_launch_trigger",
    "translate_canvas_workspace_action",
]

INITIAL_LAUNCH_PHRASES = (
    "launch askfinance agent",
    "launch askfinance",
    "launch ask finance",
    "open askfinance",
    "open ask finance",
    "launch interactive ask finance workspace",
)


def extract_genuine_user_text(llm_request: LlmRequest) -> str:
    """Extracts the latest genuine user message, skipping ADK synthetic `For context:` parts."""
    contents = getattr(llm_request, "contents", None) or []
    for content in reversed(contents):
        if getattr(content, "role", None) != "user":
            continue
        texts: list[str] = []
        for p in getattr(content, "parts", None) or []:
            txt = getattr(p, "text", None)
            if txt and not is_adk_internal_context_text(txt):
                texts.append(str(txt))
        if texts:
            return "\n".join(texts).strip()
    return ""


def _rewrite_latest_genuine_user_part(llm_request: LlmRequest, rewritten_text: str) -> None:
    """Replaces the latest genuine `user` content part (never ADK's `For context:` block)."""
    contents = getattr(llm_request, "contents", None) or []
    for content in reversed(contents):
        if getattr(content, "role", None) != "user":
            continue
        parts = getattr(content, "parts", None) or []
        has_genuine_text = any(
            getattr(p, "text", None) and not is_adk_internal_context_text(str(getattr(p, "text", "")))
            for p in parts
        )
        if has_genuine_text or not parts:
            content.parts = [types.Part.from_text(text=rewritten_text)]
            return


def _extract_canvas_event_payload(
    user_text: str,
) -> tuple[Optional[str], dict[str, Any], dict[str, Any]]:
    """Extracts `(event_name, context_dict, data_model_dict)` from an A2UI event string."""
    try:
        start = user_text.find("{")
        end = user_text.rfind("}")
        if start == -1 or end <= start:
            return None, {}, {}
        parsed = json.loads(user_text[start : end + 1])
        if not isinstance(parsed, dict):
            return None, {}, {}

        action_obj = (
            parsed.get("event")
            or parsed.get("userAction")
            or parsed.get("action", {}).get("event")
            or parsed.get("action")
            or parsed.get("data", {}).get("userAction")
            or parsed.get("data", {}).get("event")
            or parsed
        )
        event_name = action_obj.get("name") if isinstance(action_obj, dict) else None
        ctx = action_obj.get("context") if isinstance(action_obj, dict) else {}
        data_model = (
            parsed.get("dataModel")
            or parsed.get("data", {}).get("dataModel")
            or {}
        )
        return (
            str(event_name) if event_name else None,
            ctx if isinstance(ctx, dict) else {},
            data_model if isinstance(data_model, dict) else {},
        )
    except Exception:
        return None, {}, {}


def is_initial_launch_trigger(user_text: str) -> bool:
    """Returns `True` if `user_text` is the initial Canvas launch or a Clear View reset."""
    if not user_text or is_adk_internal_context_text(user_text):
        return False
    lowered = user_text.strip().lower()
    if any(phrase in lowered for phrase in INITIAL_LAUNCH_PHRASES):
        return True
    event_name, ctx, data_model = _extract_canvas_event_payload(user_text)
    if event_name == "canvas_nav_home":
        return True
    if event_name == "submit_canvas_chat_prompt":
        raw_prompt = ctx.get("prompt")
        if not isinstance(raw_prompt, str) or not raw_prompt.strip():
            raw_prompt = (data_model.get("canvasChat") or {}).get("prompt", "")
        if not str(raw_prompt or "").strip():
            return True
    return False


def translate_canvas_workspace_action(
    user_text: str,
    llm_request: LlmRequest,
    state: Any,
) -> tuple[str, str]:
    """Maps App-in-Canvas composer/chip/card events to `(effective_llm_prompt, display_label)`."""
    if not user_text or is_adk_internal_context_text(user_text):
        fallback = (
            sanitize_display_text(str(state.get("last_user_query") or ""))
            if state is not None
            else ""
        )
        fallback_label = (
            sanitize_display_text(str(state.get("canvas_display_user_query") or fallback))
            if state is not None
            else fallback
        )
        return fallback, fallback_label

    event_name, ctx, data_model = _extract_canvas_event_payload(user_text)
    if not event_name:
        clean = sanitize_display_text(user_text)
        return clean, clean

    if event_name == "submit_canvas_chat_prompt":
        raw_prompt = ctx.get("prompt")
        if not isinstance(raw_prompt, str) or not raw_prompt.strip():
            raw_prompt = (data_model.get("canvasChat") or {}).get("prompt", "")
        typed = sanitize_display_text(str(raw_prompt or ""))
        if typed:
            _rewrite_latest_genuine_user_part(llm_request, typed)
            return typed, typed

    if event_name == "select_followup_chip":
        chip_prompt = ctx.get("chipPrompt")
        if not isinstance(chip_prompt, str) or not chip_prompt.strip():
            chip_prompt = (data_model.get("canvasChat") or {}).get("selectedChip", "")
        chosen = sanitize_display_text(str(chip_prompt or ""))
        if chosen:
            _rewrite_latest_genuine_user_part(llm_request, chosen)
            return chosen, chosen

    if event_name == "open_kb_in_canvas":
        kb_id = ctx.get("kbArticleId")
        if not isinstance(kb_id, str) or not kb_id.strip():
            kb_id = (data_model.get("kbPicker") or {}).get("kbArticleId", "KB0010101")
        kb_id_str = str(kb_id or "KB0010101").strip()
        prompt = f"Call `open_interactive_coupa_kb_canvas` for article `{kb_id_str}`."
        _rewrite_latest_genuine_user_part(llm_request, prompt)
        return prompt, f"Open Policy Guide {kb_id_str}"

    if event_name == "submit_invoice_quick_lookup":
        ident = ctx.get("identifier")
        if not isinstance(ident, str):
            ident = (data_model.get("quickLookup") or {}).get("identifier", "")
        supp = ctx.get("supplierName")
        if not isinstance(supp, str):
            supp = (data_model.get("quickLookup") or {}).get("supplierName", "")
        ident_str = str(ident or "").strip()
        supp_str = str(supp or "").strip()
        if ident_str or supp_str:
            target = ident_str or supp_str
            extra = f" for supplier {supp_str}" if (ident_str and supp_str) else ""
            prompt = f"Check Coupa and Oracle status for `{target}`{extra}."
            _rewrite_latest_genuine_user_part(llm_request, prompt)
            return prompt, f"Diagnostic Lookup: {target}{extra}"

    if event_name == "confirm_create_servicenow_case":
        ref = ctx.get("referenceNumber")
        if not isinstance(ref, str):
            ref = (data_model.get("snTicket") or {}).get("referenceNumber", "902158")
        return user_text, f"Confirm & Submit ServiceNow Case (Ref: {ref})"

    if event_name == "request_servicenow_intake_form":
        ref = str(ctx.get("referenceNumber") or "Custom Inquiry")
        prompt = f"Call `stage_servicenow_case_intake_form` for reference `{ref}`."
        _rewrite_latest_genuine_user_part(llm_request, prompt)
        return prompt, f"Stage ServiceNow Intake Form ({ref})"

    if event_name == "request_urgent_early_payment":
        inv = str(ctx.get("invoiceNumber") or "INV-902605")
        supp = str(ctx.get("supplierName") or "FieldSync Ops Ltd")
        prompt = f"Call `open_urgent_payment_acceleration_form` for invoice `{inv}` ({supp})."
        _rewrite_latest_genuine_user_part(llm_request, prompt)
        return prompt, f"Request Urgent Payment ({inv})"

    return user_text, f"Action: {event_name}"


def canvas_before_model_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
    """Handles instant launch, fullscreen toggle, event translation, and model routing."""
    state = callback_context.state
    agent_name = getattr(callback_context, "agent_name", "ask_finance_canvas_orchestrator")

    with start_agent_span("gbs.canvas_turn", {"agent.name": agent_name}):
        compactor = get_compaction_manager()
        est_tokens = compactor.compact_llm_request(llm_request)

        raw_user_text = extract_genuine_user_text(llm_request)
        current_fs = bool(state.get("canvas_auto_fullscreen", True)) if state is not None else True

        # 1. Instant Full-Screen / Split-Screen Toggle (`toggle_canvas_fullscreen`)
        event_name, ctx, _ = _extract_canvas_event_payload(raw_user_text)
        if agent_name == "ask_finance_canvas_orchestrator" and event_name == "toggle_canvas_fullscreen":
            desired_fs = ctx.get("fullscreen")
            new_fs = bool(desired_fs) if isinstance(desired_fs, bool) else (not current_fs)
            if state is not None:
                state["canvas_auto_fullscreen"] = new_fs
                state["last_a2ui_surface_id"] = APP_CANVAS_SURFACE_ID

            has_active_turn = bool(
                state is not None
                and (
                    state.get("last_rendered_user_query")
                    or state.get("last_rendered_assistant_text")
                    or state.get("canvas_chat_history")
                )
            )
            if has_active_turn and state is not None:
                hist = list(state.get("canvas_chat_history") or [])
                prior = hist[:-1] if len(hist) >= 1 else []
                toggled_pkg = build_unified_app_in_canvas_surface(
                    user_query=str(state.get("last_rendered_user_query") or ""),
                    assistant_text=str(state.get("last_rendered_assistant_text") or ""),
                    pending_surfaces=filter_prompt_display_surfaces(
                        list(state.get("last_rendered_pending_surfaces") or [])
                    ),
                    prior_turns=prior,
                    initial_launch_only=False,
                    auto_fullscreen=new_fs,
                )
            else:
                toggled_pkg = build_unified_app_in_canvas_surface(
                    initial_launch_only=True,
                    auto_fullscreen=new_fs,
                )
            mode_label = "Full-Screen" if new_fs else "Split-Screen"
            parts: list[types.Part] = [
                types.Part.from_text(text=f"{HELPDESK_CARD_TITLE} switched to {mode_label} view."),
                *build_genai_a2ui_parts(toggled_pkg),
            ]
            return LlmResponse(content=types.Content(role="model", parts=parts))

        # 2. Instant minimal Turn-1 launch (or Clear View): ONLY show `AskFinance Agent` card!
        if agent_name == "ask_finance_canvas_orchestrator" and is_initial_launch_trigger(raw_user_text):
            if state is not None:
                state["pending_a2ui_parts"] = []
                state["last_rendered_pending_surfaces"] = []
                state["last_rendered_user_query"] = ""
                state["last_rendered_assistant_text"] = ""
                state["canvas_chat_history"] = []
                state["canvas_initial_launch_only"] = False
                state["last_a2ui_surface_id"] = APP_CANVAS_SURFACE_ID
            minimal_pkg = build_unified_app_in_canvas_surface(
                initial_launch_only=True,
                auto_fullscreen=current_fs,
            )
            launch_parts: list[types.Part] = [
                types.Part.from_text(text=f"{HELPDESK_CARD_TITLE} is open in Canvas."),
                *build_genai_a2ui_parts(minimal_pkg),
            ]
            return LlmResponse(content=types.Content(role="model", parts=launch_parts))

        if state is not None:
            state["canvas_initial_launch_only"] = False

        confirmed = parse_a2ui_user_action_event(raw_user_text, state)
        effective_query, display_label = translate_canvas_workspace_action(
            raw_user_text,
            llm_request,
            state,
        )
        if not confirmed and effective_query != raw_user_text:
            confirmed = parse_a2ui_user_action_event(effective_query, state)

        decision = get_model_router().route_query(effective_query)
        exec_id = f"mdl-{uuid.uuid4().hex[:12]}"

        if agent_name == "ask_finance_canvas_orchestrator" and hasattr(llm_request, "model"):
            llm_request.model = decision.selected_model

        if state is not None:
            state["current_model_exec_id"] = exec_id
            state["current_model_start_ts"] = time.perf_counter()
            state["routed_model"] = decision.selected_model
            state["routed_complexity_tier"] = decision.complexity_tier
            # Only update user query/label on orchestrator entry (or when unset) and NEVER with ADK context
            if (
                agent_name == "ask_finance_canvas_orchestrator"
                or not state.get("canvas_display_user_query")
            ):
                if effective_query and not is_adk_internal_context_text(effective_query):
                    state["last_user_query"] = effective_query
                if display_label and not is_adk_internal_context_text(display_label):
                    state["canvas_display_user_query"] = display_label

        get_intent_outcome_tracker().log_intent(
            execution_id=exec_id,
            agent_name=agent_name,
            tool_name=f"llm_generate:{decision.selected_model}",
            sanitized_args={
                "user_query": effective_query[:300],
                "compacted_est_tokens": est_tokens,
                "hitl_confirmed_event": confirmed,
            },
            reason=decision.routing_reason,
            session_id=getattr(callback_context, "invocation_id", None),
        )
    return None


def canvas_after_model_callback(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> Optional[LlmResponse]:
    """Attaches unified Canvas surface, logs model OUTCOME, and consolidates memory."""
    if getattr(llm_response, "partial", False):
        return None

    state = callback_context.state
    agent_name = getattr(callback_context, "agent_name", "ask_finance_canvas_orchestrator")
    exec_id = (
        state.get("current_model_exec_id", f"mdl-{uuid.uuid4().hex[:8]}")
        if state is not None
        else f"mdl-{uuid.uuid4().hex[:8]}"
    )
    start_ts = (
        float(state.get("current_model_start_ts", time.perf_counter()))
        if state is not None
        else time.perf_counter()
    )
    latency_ms = max(0.1, (time.perf_counter() - start_ts) * 1000.0)

    content = getattr(llm_response, "content", None)
    parts = list(getattr(content, "parts", None) or []) if content else []
    merged_assistant_text = sanitize_display_text(
        " ".join(str(p.text) for p in parts if getattr(p, "text", None))
    )

    current_fs = bool(state.get("canvas_auto_fullscreen", True)) if state is not None else True
    attach_app_in_canvas_to_response(
        callback_context=callback_context,
        llm_response=llm_response,
        auto_fullscreen=current_fs,
    )

    get_intent_outcome_tracker().log_outcome(
        execution_id=exec_id,
        agent_name=agent_name,
        tool_name=f"llm_generate:{state.get('routed_model', 'gemini-3.8-flash') if state else 'gemini-3.8-flash'}",
        status="SUCCESS",
        latency_ms=latency_ms,
        a2ui_surface_id=APP_CANVAS_SURFACE_ID,
        summary=merged_assistant_text[:240] or "Delegated / tool turn emitted.",
        session_id=getattr(callback_context, "invocation_id", None),
    )

    if merged_assistant_text:
        session_id = str(getattr(callback_context, "invocation_id", None) or "default-canvas-session")
        last_user = sanitize_display_text(str(state.get("last_user_query", "")) if state is not None else "")
        turns_to_consolidate = []
        if last_user:
            turns_to_consolidate.append({"role": "user", "content": last_user})
        turns_to_consolidate.append({"role": "model", "content": merged_assistant_text})
        schedule_async_memory_consolidation(session_id=session_id, turns=turns_to_consolidate)

    return None


def canvas_after_tool_callback(
    tool: Any,
    args: dict[str, Any],
    tool_context: Any,
    tool_response: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Logs post-execution tool OUTCOME and drops any `Cross-System Coupa & Oracle Tracker` card."""
    state = getattr(tool_context, "state", None) if tool_context is not None else None
    if state is not None:
        pending = list(state.get("pending_a2ui_parts") or [])
        filtered = filter_prompt_display_surfaces(pending)
        if len(filtered) != len(pending):
            state["pending_a2ui_parts"] = filtered
            if str(state.get("last_a2ui_surface_id") or "").startswith("gbs-invoice-status-"):
                state["last_a2ui_surface_id"] = APP_CANVAS_SURFACE_ID

    if isinstance(tool_response, dict) and str(tool_response.get("a2ui_surface_id") or "").startswith(
        "gbs-invoice-status-"
    ):
        updated_resp = dict(tool_response)
        updated_resp["a2ui_surface_id"] = APP_CANVAS_SURFACE_ID
        after_tool_callback(tool, args, tool_context, updated_resp)
        return updated_resp

    return after_tool_callback(tool, args, tool_context, tool_response)
