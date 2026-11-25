"""Real-Time A2A Streaming Status Indicator Executor (`ask_finance_canvas_agent`).

Wraps ONLY `/a2a/ask_finance_canvas_agent`'s `A2aAgentExecutor` so that the instant a user
clicks **Send** (or clicks a follow-up chip / form action) inside Canvas, the server
immediately streams (<20ms over SSE) an AskFinance Agent `gbs-app-in-canvas` working card
(`MaterialProgressSpinner` + indeterminate `MaterialProgressBar` + disabled `Working...`
button), updates the status headline as specialist sub-agents/tools execute, and cleanly
replaces the same `artifact_id` in-place once the final response and UI cards finish.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import Artifact, Message, TaskArtifactUpdateEvent, TaskStatusUpdateEvent
from google.adk.a2a import _compat
from google.adk.a2a.converters.part_converter import convert_genai_part_to_a2a_part
from google.genai import types

from ask_finance_agent.observability.structured_logger import get_structured_logger
from ask_finance_agent.plugins.a2ui_canvas_plugin import A2UI_MIME_TYPE, build_genai_a2ui_parts

from .callbacks import _extract_canvas_event_payload, is_initial_launch_trigger
from .canvas_workspace_plugin import (
    APP_CANVAS_SURFACE_ID,
    build_working_status_app_in_canvas_surface,
    sanitize_display_text,
)

_LOGGER = get_structured_logger("ask_finance_canvas_agent.streaming_executor")

_STAGE_LABELS: tuple[tuple[str, str, str], ...] = (
    (
        "reconcile_multi_invoice_statement",
        "Reconciling multi-invoice statement...",
        "Auditing statement lines across Coupa POs and Oracle EFT payment ledgers...",
    ),
    (
        "multi_invoice_reconciliation_workflow",
        "Running statement reconciliation workflow...",
        "Coordinating cross-system statement audit and P1 escalation rules...",
    ),
    (
        "lookup_invoice_payment_status",
        "Checking Coupa & Oracle Financials...",
        "Retrieving invoice approval state, 3-way match status, and Net 30 EFT schedule...",
    ),
    (
        "invoice_payment_diagnostic_agent",
        "Routing to Invoice & Payment Diagnostic Specialist...",
        "Querying Coupa procurement records and Oracle Financials payment status...",
    ),
    (
        "search_coupa_procurement_kb",
        "Searching Coupa Policy Knowledge Base...",
        "Retrieving verified GBS standard operating procedures and policy citations...",
    ),
    (
        "open_interactive_coupa_kb_canvas",
        "Preparing Interactive Policy Guide...",
        "Formatting official GBS Knowledge Base procedures and checklists...",
    ),
    (
        "procurement_kb_advisory_agent",
        "Routing to Coupa Procurement Policy Specialist...",
        "Searching indexed GBS Knowledge Base articles and compliance rules...",
    ),
    (
        "coupa_procurement_rag_agent",
        "Routing to Coupa Procurement Policy Specialist...",
        "Searching indexed GBS Knowledge Base articles and compliance rules...",
    ),
    (
        "submit_confirmed_servicenow_case",
        "Submitting confirmed ServiceNow case...",
        "Generating specialist assignment receipt and SLA confirmation...",
    ),
    (
        "stage_servicenow_case_intake_form",
        "Staging ServiceNow Specialist Intake Form...",
        "Pre-populating GBS service queue, priority SLA, and reference details...",
    ),
    (
        "open_urgent_payment_acceleration_form",
        "Opening Urgent Payment Acceleration Form...",
        "Preparing Treasury early-release intake fields per KB0010108...",
    ),
    (
        "servicenow_escalation_hitl_agent",
        "Routing to ServiceNow Escalation Specialist...",
        "Evaluating human-in-the-loop handoff requirements and SLA routing...",
    ),
)


def _extract_raw_a2a_message_text(message: Optional[Message]) -> str:
    """Extracts text and JSON DataPart payloads from an incoming A2A `Message`."""
    if message is None or not getattr(message, "parts", None):
        return ""
    chunks: list[str] = []
    for part in message.parts:
        try:
            if _compat.is_text_part(part):
                txt = _compat.part_text(part)
                if txt:
                    chunks.append(txt)
            elif _compat.is_data_part(part):
                data = _compat.data_part_dict(part)
                if data:
                    chunks.append(json.dumps(data, ensure_ascii=False))
        except Exception:
            continue
    return "\n".join(chunks).strip()


def _resolve_friendly_prompt_preview(raw_text: str) -> str:
    """Derives a clean user-facing prompt summary for the immediate working card."""
    if not raw_text:
        return "Processing inquiry..."

    event_name, ctx, data_model = _extract_canvas_event_payload(raw_text)
    if not event_name:
        return sanitize_display_text(raw_text) or "Processing inquiry..."

    if event_name == "submit_canvas_chat_prompt":
        prompt = ctx.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            prompt = (data_model.get("canvasChat") or {}).get("prompt", "")
        return sanitize_display_text(str(prompt or "")) or "Processing inquiry..."

    if event_name == "select_followup_chip":
        chip = ctx.get("chipPrompt")
        if not isinstance(chip, str) or not chip.strip():
            chip = (data_model.get("canvasChat") or {}).get("selectedChip", "")
        return sanitize_display_text(str(chip or "")) or "Following up on selected action..."

    if event_name == "submit_invoice_quick_lookup":
        ident = ctx.get("identifier")
        if not isinstance(ident, str):
            ident = (data_model.get("quickLookup") or {}).get("identifier", "")
        supp = ctx.get("supplierName")
        if not isinstance(supp, str):
            supp = (data_model.get("quickLookup") or {}).get("supplierName", "")
        target = str(ident or supp or "Invoice Inquiry").strip()
        return f"Diagnostic Lookup: {target}"

    if event_name == "open_kb_in_canvas":
        kb_id = ctx.get("kbArticleId")
        if not isinstance(kb_id, str) or not kb_id.strip():
            kb_id = (data_model.get("kbPicker") or {}).get("kbArticleId", "KB0010101")
        return f"Open Policy Guide {str(kb_id).strip()}"

    if event_name == "confirm_create_servicenow_case":
        ref = ctx.get("referenceNumber")
        if not isinstance(ref, str):
            ref = (data_model.get("snTicket") or {}).get("referenceNumber", "")
        return f"Submitting ServiceNow Case{f' (Ref: {ref})' if ref else ''}"

    if event_name == "request_servicenow_intake_form":
        ref = str(ctx.get("referenceNumber") or "Support Case").strip()
        return f"Opening ServiceNow Intake Form ({ref})"

    if event_name == "request_urgent_early_payment":
        inv = str(ctx.get("invoiceNumber") or "Invoice").strip()
        return f"Opening Urgent Payment Request ({inv})"

    return f"Processing action: {event_name}"


def _build_working_a2a_parts(
    *,
    user_query: str,
    stage_headline: str,
    stage_detail: str,
    prior_turns: list[dict[str, str]],
    auto_fullscreen: bool,
) -> list[Any]:
    """Converts a working-status `gbs-app-in-canvas` surface into A2A `Part`s."""
    pkg = build_working_status_app_in_canvas_surface(
        user_query=user_query,
        stage_headline=stage_headline,
        stage_detail=stage_detail,
        prior_turns=prior_turns,
        auto_fullscreen=auto_fullscreen,
    )
    genai_parts = [
        types.Part.from_text(text=f"⏳ {stage_headline}"),
        *build_genai_a2ui_parts(pkg),
    ]
    a2a_parts: list[Any] = []
    for gp in genai_parts:
        converted = convert_genai_part_to_a2a_part(gp)
        if isinstance(converted, list):
            a2a_parts.extend(converted)
        elif converted is not None:
            a2a_parts.append(converted)
    return a2a_parts


def _message_has_final_canvas_surface(message: Optional[Message]) -> bool:
    """Returns `True` if `message` already carries `gbs-app-in-canvas` A2UI DataParts."""
    if message is None or not getattr(message, "parts", None):
        return False
    for part in message.parts:
        try:
            meta = _compat.part_metadata(part) or {}
            if meta.get("mimeType") == A2UI_MIME_TYPE and meta.get("surface_id") == APP_CANVAS_SURFACE_ID:
                return True
            if _compat.is_data_part(part):
                d = _compat.data_part_dict(part) or {}
                cs = d.get("createSurface") if isinstance(d, dict) else None
                if isinstance(cs, dict) and cs.get("surfaceId") == APP_CANVAS_SURFACE_ID:
                    return True
        except Exception:
            continue
    return False


def _detect_intermediate_stage(message: Optional[Message]) -> Optional[tuple[str, str]]:
    """Inspects an intermediate A2A `Message` (e.g. `transfer_to_agent` / tool call) for stage info."""
    if message is None or not getattr(message, "parts", None):
        return None
    serialized = _extract_raw_a2a_message_text(message)
    if not serialized:
        return None
    for token, headline, detail in _STAGE_LABELS:
        if token in serialized:
            return headline, detail
    return None


class _CanvasProgressEventQueueProxy:
    """Proxies `EventQueue` to stream immediate & mid-flight A2UI working indicators."""

    def __init__(
        self,
        inner_queue: EventQueue,
        *,
        task_id: str,
        context_id: str,
        user_query: str,
        prior_turns: list[dict[str, str]],
        auto_fullscreen: bool,
    ) -> None:
        self._inner = inner_queue
        self._task_id = task_id
        self._context_id = context_id
        self._shared_artifact_id = f"canvas-{task_id}"
        self._user_query = user_query
        self._prior_turns = prior_turns
        self._auto_fullscreen = auto_fullscreen
        self._emitted_initial_working = False
        self._emitted_headlines: set[str] = set()
        self._final_surface_seen = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def enqueue_event(self, event: Any) -> None:
        try:
            # 1. Final or intermediate TaskArtifactUpdateEvent -> unify artifact_id so it replaces cleanly
            if isinstance(event, TaskArtifactUpdateEvent):
                event.artifact.artifact_id = self._shared_artifact_id
                event.append = False
                if event.last_chunk:
                    self._final_surface_seen = True
                await self._inner.enqueue_event(event)
                return

            # 2. TaskStatusUpdateEvent in WORKING state
            if (
                isinstance(event, TaskStatusUpdateEvent)
                and event.status.state == _compat.TS_WORKING
                and not self._final_surface_seen
            ):
                existing_msg = event.status.message if event.status.HasField("message") else None
                if _message_has_final_canvas_surface(existing_msg):
                    self._final_surface_seen = True
                    await self._inner.enqueue_event(event)
                    return

                # 2a. Very first WORKING signal (<20ms after Send click, BEFORE LLM call!)
                if not self._emitted_initial_working:
                    self._emitted_initial_working = True
                    headline = "Working on your request..."
                    detail = (
                        "AskFinance Agent received your prompt and is routing across Coupa, "
                        "Oracle Financials, and ServiceNow..."
                    )
                    self._emitted_headlines.add(headline)
                    working_parts = _build_working_a2a_parts(
                        user_query=self._user_query,
                        stage_headline=headline,
                        stage_detail=detail,
                        prior_turns=self._prior_turns,
                        auto_fullscreen=self._auto_fullscreen,
                    )
                    working_msg = Message(
                        message_id=str(uuid.uuid4()),
                        role=_compat.ROLE_AGENT,
                        parts=working_parts,
                    )
                    enriched_status = _compat.make_task_status_update_event(
                        task_id=self._task_id,
                        context_id=self._context_id,
                        status=_compat.make_task_status(_compat.TS_WORKING, message=working_msg),
                        final=False,
                    )
                    await self._inner.enqueue_event(enriched_status)
                    await self._inner.enqueue_event(
                        TaskArtifactUpdateEvent(
                            task_id=self._task_id,
                            context_id=self._context_id,
                            append=False,
                            last_chunk=False,
                            artifact=Artifact(
                                artifact_id=self._shared_artifact_id,
                                parts=working_parts,
                            ),
                        )
                    )
                    return

                # 2b. Mid-flight sub-agent transfer or tool call milestone
                stage_info = _detect_intermediate_stage(existing_msg)
                if stage_info is not None:
                    headline, detail = stage_info
                    if headline not in self._emitted_headlines:
                        self._emitted_headlines.add(headline)
                        step_parts = _build_working_a2a_parts(
                            user_query=self._user_query,
                            stage_headline=headline,
                            stage_detail=detail,
                            prior_turns=self._prior_turns,
                            auto_fullscreen=self._auto_fullscreen,
                        )
                        await self._inner.enqueue_event(event)
                        await self._inner.enqueue_event(
                            TaskArtifactUpdateEvent(
                                task_id=self._task_id,
                                context_id=self._context_id,
                                append=False,
                                last_chunk=False,
                                artifact=Artifact(
                                    artifact_id=self._shared_artifact_id,
                                    parts=step_parts,
                                ),
                            )
                        )
                        return
        except Exception as exc:
            _LOGGER.warning(
                "Canvas progress streaming proxy fell back safely",
                error_type=type(exc).__name__,
            )

        await self._inner.enqueue_event(event)


class CanvasStreamingProgressExecutor(AgentExecutor):
    """Decorator `AgentExecutor` emitting instant A2UI working feedback for `ask_finance_canvas_agent`."""

    def __init__(self, delegate: AgentExecutor) -> None:
        super().__init__()
        self._delegate = delegate

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await self._delegate.cancel(context, event_queue)

    async def _peek_session_ui_context(
        self,
        context: RequestContext,
    ) -> tuple[list[dict[str, str]], bool]:
        """Reads `(prior_turns, auto_fullscreen)` from existing ADK session state if available."""
        try:
            resolve_runner = getattr(self._delegate, "_resolve_runner", None)
            cfg = getattr(self._delegate, "_config", None)
            if callable(resolve_runner) and cfg is not None:
                runner = await resolve_runner()
                run_req = cfg.request_converter(context, cfg.a2a_part_converter)
                if run_req.session_id:
                    sess = await runner.session_service.get_session(
                        app_name=runner.app_name,
                        user_id=run_req.user_id,
                        session_id=run_req.session_id,
                    )
                    if sess is not None and sess.state is not None:
                        hist = list(sess.state.get("canvas_chat_history") or [])
                        fs = bool(sess.state.get("canvas_auto_fullscreen", True))
                        return hist, fs
        except Exception:
            pass
        return [], True

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        raw_text = _extract_raw_a2a_message_text(context.message)
        event_name, _, _ = _extract_canvas_event_payload(raw_text)

        # Skip working card for <20ms instant short-circuit actions (initial launch / fullscreen toggle)
        if (
            not raw_text
            or is_initial_launch_trigger(raw_text)
            or event_name == "toggle_canvas_fullscreen"
            or not context.task_id
            or not context.context_id
        ):
            await self._delegate.execute(context, event_queue)
            return

        display_query = _resolve_friendly_prompt_preview(raw_text)
        prior_turns, auto_fs = await self._peek_session_ui_context(context)
        proxied_queue = _CanvasProgressEventQueueProxy(
            event_queue,
            task_id=str(context.task_id),
            context_id=str(context.context_id),
            user_query=display_query,
            prior_turns=prior_turns,
            auto_fullscreen=auto_fs,
        )
        await self._delegate.execute(context, proxied_queue)  # type: ignore[arg-type]


def install_canvas_streaming_progress(app: Any) -> bool:
    """Attaches `CanvasStreamingProgressExecutor` ONLY to `/a2a/ask_finance_canvas_agent`."""
    installed = False
    for route in getattr(getattr(app, "router", None), "routes", []) or []:
        if getattr(route, "path", None) != "/a2a/ask_finance_canvas_agent":
            continue
        endpoint = getattr(route, "endpoint", None)
        dispatcher = getattr(endpoint, "__self__", None)
        handler = getattr(dispatcher, "request_handler", None)
        current_exec = getattr(handler, "agent_executor", None)
        if handler is not None and current_exec is not None and not isinstance(
            current_exec, CanvasStreamingProgressExecutor
        ):
            wrapped = CanvasStreamingProgressExecutor(current_exec)
            handler.agent_executor = wrapped
            registry = getattr(handler, "_active_task_registry", None)
            if registry is not None and hasattr(registry, "_agent_executor"):
                registry._agent_executor = wrapped
            installed = True
    return installed
