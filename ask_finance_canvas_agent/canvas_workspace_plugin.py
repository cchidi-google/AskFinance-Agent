"""App-in-Canvas A2UI v0.9 Workspace Plugin (`ask_finance_canvas_agent`).

Transforms the conversational experience into a clean, minimal full-screen A2UI v0.9
`Canvas` (`surfaceId = "gbs-app-in-canvas"`, `autoOpen = True`) styled in a crisp
Green (`#008768`), Black (`#111111`), and White (`#FFFFFF`) color palette:
1. **On Initial Launch (unprompted)**: Renders ONLY the **`AskFinance Agent`**
   prompt card (`MaterialInput` with client-side `checks`, `MaterialButton`, and a
   Full-Screen / Split-Screen `MaterialIconButton` toggle in the black header).
   Zero unprompted cards are displayed.
2. **When Prompted**:
   - **First**: Renders the **Agent Text Response Card** (`ws-chat-card`) with sanitized
     text (stripping internal ADK `For context:` / `<<<BEGIN_QUOTED_AGENT_CONTENT>>>`
     sub-agent handoff blocks) plus contextual next-best-action `MaterialChips`.
   - **Second (Below Text Response)**: Renders any prompted interactive `MaterialCard`s
     (form inputs, `MaterialBadge` + SLA pills, KB accordions, and `MaterialTabs`
     multi-panel reconciliations), while strictly excluding the redundant
     `Cross-System Coupa & Oracle Tracker` (`gbs-invoice-status-*`) card from all
     prompt response display outputs.
   - **Third (Bottom)**: Keeps the **`AskFinance Agent`** composer card docked below.
"""

from __future__ import annotations

import base64
import copy
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins import BasePlugin
from google.genai import types

from ask_finance_agent.observability.structured_logger import get_structured_logger
from ask_finance_agent.plugins.a2ui_canvas_plugin import (
    A2UI_VERSION,
    GE_COMPOSITE_CATALOG_ID,
    _clamp_title,
    build_genai_a2ui_parts,
    package_a2ui_datapart,
)

APP_CANVAS_SURFACE_ID = "gbs-app-in-canvas"
HELPDESK_CARD_TITLE = "AskFinance Agent"
MAX_HISTORY_TURNS = 5

# Brand Color Palette (Green, Black, and White)
BRAND_GREEN = "#008768"
BRAND_GREEN_TINT = "#E6F4F0"
BRAND_BLACK = "#111111"
BRAND_WHITE = "#FFFFFF"
BRAND_CANVAS_BG = "#F5F8F7"
BRAND_BORDER = "#CBDAD5"
BRAND_DIVIDER = "#E2ECE9"
BRAND_CHARCOAL = "#3D4845"

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_LOGGER = get_structured_logger("ask_finance_canvas_agent.workspace_plugin")

_ADK_CONTEXT_BLOCK_RE = re.compile(
    r"For context:\s*below is a transcript of what another agent did[\s\S]*<<<END_QUOTED_AGENT_CONTENT>>>",
    re.IGNORECASE,
)
_ADK_PREAMBLE_RE = re.compile(
    r"Everything between those markers is data for you to read[\s\S]*?from the user\.",
    re.IGNORECASE,
)
_QUOTED_AGENT_BLOCK_RE = re.compile(
    r"<<<BEGIN_QUOTED_AGENT_CONTENT>>>[\s\S]*?<<<END_QUOTED_AGENT_CONTENT>>>",
    re.IGNORECASE,
)
_ADK_UNCLOSED_FOR_CONTEXT_RE = re.compile(
    r"For context:\s*below is a transcript of what another agent did[\s\S]*$",
    re.IGNORECASE,
)
_TRANSFER_TOOL_LINE_RE = re.compile(
    r"\[[A-Za-z0-9_.-]+\]\s+[A-Za-z0-9_]+\s+tool returned result:[^\n]*",
    re.IGNORECASE,
)
_RENDERED_SURFACE_MARKER_RE = re.compile(r"\[Rendered A2UI Surface:[^\]]*\]", re.IGNORECASE)
_RAW_ACTION_EVENT_RE = re.compile(r"\[A2UI User Action Event\][\s\S]*$", re.IGNORECASE)
_COMPACTED_SUMMARY_RE = re.compile(r"\[COMPACTED SESSION HISTORY SUMMARY\][\s\S]*$", re.IGNORECASE)
_INVOICE_TOKEN_RE = re.compile(r"\b(INV\d{5,}|902158|923469|923104|923288|PO\s*#?[A-Z0-9-]+)\b", re.IGNORECASE)


def is_adk_internal_context_text(text: str) -> bool:
    """Returns `True` if `text` is a synthetic ADK sub-agent handoff/compaction block."""
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    lower = stripped.lower()
    return (
        "<<<begin_quoted_agent_content>>>" in lower
        or "<<<end_quoted_agent_content>>>" in lower
        or lower.startswith("for context:")
        or "below is a transcript of what another agent did" in lower
        or "transfer_to_agent tool returned result" in lower
        or lower.startswith("[compacted session history summary]")
    )


def sanitize_display_text(text: str) -> str:
    """Strips internal ADK sub-agent transfer transcripts and A2UI wire markers."""
    if not text:
        return ""
    cleaned = _ADK_CONTEXT_BLOCK_RE.sub("", text)
    cleaned = _ADK_PREAMBLE_RE.sub("", cleaned)
    cleaned = _QUOTED_AGENT_BLOCK_RE.sub("", cleaned)
    cleaned = _ADK_UNCLOSED_FOR_CONTEXT_RE.sub("", cleaned)
    cleaned = _TRANSFER_TOOL_LINE_RE.sub("", cleaned)
    cleaned = _RENDERED_SURFACE_MARKER_RE.sub("", cleaned)
    cleaned = _RAW_ACTION_EVENT_RE.sub("", cleaned)
    cleaned = _COMPACTED_SUMMARY_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


@lru_cache(maxsize=1)
def get_helpdesk_icon_data_uri() -> str:
    """Returns the inline `data:image/png;base64,...` URI for the custom AskFinance Agent icon."""
    for candidate in (
        "askfinance_helpdesk_icon_88.png",
        "askfinance_helpdesk_icon.png",
        "askgbs_helpdesk_icon_88.png",
        "askgbs_helpdesk_icon.png",
    ):
        icon_path = _ASSETS_DIR / candidate
        if icon_path.is_file():
            raw = icon_path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
            return f"data:image/png;base64,{b64}"
    raise FileNotFoundError("AskFinance Agent icon asset not found")


def _apply_brand_component_theme(comp: dict[str, Any], *, is_embedded_root: bool = False) -> None:
    """Applies Green/Black/White inline `style` rules using catalog-valid CSS keys."""
    ctype = comp.get("component")
    if ctype in {"Canvas", "VegaChart", "Stepper"}:
        # CustomComponentCommonProps does not take inline `style`
        return

    style = dict(comp.get("style") or {})
    if is_embedded_root and ctype == "MaterialCard":
        style.update(
            {
                "backgroundColor": BRAND_WHITE,
                "border": f"1px solid {BRAND_BORDER}",
                "borderLeft": f"4px solid {BRAND_GREEN}",
                "borderRadius": "12px",
                "boxShadow": "0 2px 8px rgba(17, 17, 17, 0.06)",
            }
        )
    elif ctype == "MaterialIcon" and comp.get("color") == "primary":
        style.setdefault("color", BRAND_GREEN)
    elif ctype == "MaterialText":
        hint = comp.get("usageHint")
        if hint in {"h3", "h4"}:
            style.setdefault("color", BRAND_BLACK)
            style.setdefault("fontWeight", "600")
        elif hint in {"body2", "caption"}:
            style.setdefault("color", BRAND_CHARCOAL)
    elif ctype == "MaterialDivider":
        style.setdefault("borderColor", BRAND_DIVIDER)
    elif ctype == "MaterialButton":
        variant = comp.get("variant")
        if variant in {"raised", "flat"} and comp.get("color") == "primary":
            style.update(
                {
                    "backgroundColor": BRAND_GREEN,
                    "color": BRAND_WHITE,
                    "fontWeight": "600",
                    "borderRadius": "6px",
                }
            )
        elif variant == "stroked":
            style.update(
                {
                    "borderColor": BRAND_BLACK,
                    "color": BRAND_BLACK,
                    "fontWeight": "500",
                    "borderRadius": "6px",
                }
            )
        elif variant == "basic":
            style.setdefault("color", BRAND_BLACK)

    if style:
        comp["style"] = style


def _apply_brand_vega_theme(dm: dict[str, Any]) -> None:
    """Themes embedded Vega-Lite specs (`/reconciliation/vegaSpec`) in Green/Black/White."""
    if dm.get("path") != "/reconciliation":
        return
    val = dm.get("value")
    if not isinstance(val, dict):
        return
    spec = val.get("vegaSpec")
    if not isinstance(spec, dict):
        return
    spec["background"] = BRAND_WHITE
    encoding = spec.get("encoding")
    if isinstance(encoding, dict) and isinstance(encoding.get("color"), dict):
        encoding["color"]["scale"] = {
            "range": [BRAND_GREEN, BRAND_CHARCOAL, BRAND_BLACK],
        }


def is_coupa_oracle_tracker_surface(surface: Any) -> bool:
    """Returns `True` if `surface` is the `Cross-System Coupa & Oracle Tracker` card."""
    if not isinstance(surface, dict):
        return False
    surface_id = str(surface.get("surface_id") or "")
    if surface_id.startswith("gbs-invoice-status-"):
        return True
    readable = str(surface.get("readable_text") or "")
    if "Coupa/Oracle Status Card" in readable or "Cross-System Coupa & Oracle Tracker" in readable:
        return True
    for msg in surface.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        cs = msg.get("createSurface")
        if isinstance(cs, dict) and str(cs.get("surfaceId") or "").startswith("gbs-invoice-status-"):
            return True
        uc = msg.get("updateComponents")
        if isinstance(uc, dict):
            for comp in uc.get("components") or []:
                if isinstance(comp, dict) and "Cross-System Coupa & Oracle Tracker" in str(
                    comp.get("text") or ""
                ):
                    return True
    return False


def filter_prompt_display_surfaces(
    surfaces: Optional[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Removes `Cross-System Coupa & Oracle Tracker` cards from any prompt display queue."""
    if not surfaces:
        return []
    return [s for s in surfaces if not is_coupa_oracle_tracker_surface(s)]


def _resolve_badge_and_pill_meta(
    surface_id: str,
    data_models: list[dict[str, Any]],
) -> tuple[str, str, str, str]:
    """Returns `(badge_short, badge_color, pill_label, pill_bg_hex)` for a prompted card."""
    priority = ""
    for dm in data_models:
        u = dm.get("updateDataModel", {})
        if u.get("path") == "/snTicket" and isinstance(u.get("value"), dict):
            priority = str(u["value"].get("priority") or "").upper()

    if surface_id == "gbs-servicenow-intake-form":
        if priority == "P1":
            return "P1", "warn", "P1 CRITICAL • 2H SLA", BRAND_BLACK
        return "P2", "primary", "HITL REVIEW • 4H SLA", BRAND_GREEN
    if surface_id.startswith("gbs-servicenow-receipt-"):
        return "✓", "primary", "CASE ASSIGNED • ACTIVE", BRAND_GREEN
    if surface_id == "gbs-missing-identifier-card":
        return "?", "primary", "IDENTIFIER REQUIRED", BRAND_BLACK
    if surface_id.startswith("gbs-urgent-payment-"):
        return "P2", "primary", "TREASURY P2 • CONTROLLER GATE", BRAND_GREEN
    if surface_id == "gbs-biodata-reconciliation-canvas":
        return "P1", "warn", "P1 LEGAL HOLD • 3 INVOICES", BRAND_BLACK
    return "KB", "primary", "OFFICIAL GBS POLICY", BRAND_GREEN


def _decorate_card_header_and_controls(
    prefix: str,
    surface_id: str,
    components: list[dict[str, Any]],
    data_models: list[dict[str, Any]],
) -> None:
    """Attaches `MaterialBadge` + SLA pill headers and input `checks` in-place."""
    comp_map = {c["id"]: c for c in components}
    badge_txt, badge_color, pill_label, pill_bg = _resolve_badge_and_pill_meta(surface_id, data_models)

    # 1. Decorate card header row with MaterialBadge around leading icon + right-aligned SLA/Confidence pill
    header_candidates = (
        f"{prefix}sn-header-row",
        f"{prefix}receipt-header",
        f"{prefix}lookup-header",
        f"{prefix}urgent-header",
    )
    for hid in header_candidates:
        hdr = comp_map.get(hid)
        if hdr and hdr.get("component") == "MaterialRow":
            children = list(hdr.get("children") or [])
            if children and comp_map.get(children[0], {}).get("component") == "MaterialIcon":
                icon_id = children[0]
                badge_id = f"{prefix}hdr-badge"
                components.append(
                    {
                        "id": badge_id,
                        "component": "MaterialBadge",
                        "text": badge_txt,
                        "color": badge_color,
                        "size": "small",
                        "overlap": False,
                        "description": pill_label,
                        "children": [icon_id],
                    }
                )
                children[0] = badge_id
            pill_id = f"{prefix}hdr-pill"
            components.append(
                {
                    "id": pill_id,
                    "component": "MaterialText",
                    "text": pill_label,
                    "usageHint": "caption",
                    "style": {
                        "backgroundColor": pill_bg,
                        "color": BRAND_WHITE,
                        "padding": "4px 10px",
                        "borderRadius": "999px",
                        "fontWeight": "600",
                        "fontSize": "11px",
                        "letterSpacing": "0.4px",
                        "marginLeft": "auto",
                    },
                }
            )
            children.append(pill_id)
            hdr["children"] = children
            break

    # 2. Add client-side validation checks on ServiceNow intake inputs
    if surface_id == "gbs-servicenow-intake-form":
        ref_input = comp_map.get(f"{prefix}sn-input-ref")
        if ref_input and ref_input.get("component") == "MaterialInput":
            ref_input["checks"] = [
                {
                    "condition": {
                        "call": "required",
                        "args": {"value": {"path": "/snTicket/referenceNumber"}},
                    },
                    "message": "Reference number is required before submitting.",
                }
            ]
        email_input = comp_map.get(f"{prefix}sn-input-email")
        if email_input and email_input.get("component") == "MaterialInput":
            email_input["checks"] = [
                {
                    "condition": {
                        "call": "email",
                        "args": {"value": {"path": "/snTicket/requesterEmail"}},
                    },
                    "message": "Enter a valid corporate requester email address.",
                }
            ]


def _embed_queued_surface_components(
    prefix: str,
    packaged_surface: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Namespaces, themes, and enriches a queued A2UI surface inside `gbs-app-in-canvas`."""
    surface_id = str(packaged_surface.get("surface_id") or "")
    messages = packaged_surface.get("messages") or []
    raw_components: list[dict[str, Any]] = []
    data_models: list[dict[str, Any]] = []

    for msg in messages:
        if "updateComponents" in msg:
            raw_components.extend(copy.deepcopy(msg["updateComponents"].get("components") or []))
        elif "updateDataModel" in msg:
            dm = copy.deepcopy(msg["updateDataModel"])
            dm["surfaceId"] = APP_CANVAS_SURFACE_ID
            _apply_brand_vega_theme(dm)
            data_models.append({"version": A2UI_VERSION, "updateDataModel": dm})

    embedded_root_id = f"{prefix}root"
    namespaced: list[dict[str, Any]] = []

    for comp in raw_components:
        orig_id = str(comp["id"])
        new_id = f"{prefix}{orig_id}"
        children = [f"{prefix}{cid}" for cid in (comp.get("children") or [])]

        if orig_id == "root" and comp.get("component") == "Canvas":
            converted: dict[str, Any] = {
                "id": new_id,
                "component": "MaterialCard",
                "appearance": "outlined",
                "children": children,
            }
            _apply_brand_component_theme(converted, is_embedded_root=True)
            namespaced.append(converted)
        else:
            updated = copy.deepcopy(comp)
            updated["id"] = new_id
            if "children" in updated:
                updated["children"] = children
            _apply_brand_component_theme(updated, is_embedded_root=(orig_id == "root"))
            namespaced.append(updated)

    _decorate_card_header_and_controls(prefix, surface_id, namespaced, data_models)
    return embedded_root_id, namespaced, data_models


def _build_tabbed_reconciliation_card(
    recon_surface: dict[str, Any],
    sn_surface: Optional[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Composes `gbs-biodata-reconciliation-canvas` (+ optional P1 form) into `MaterialTabs`."""
    _, recon_comps, recon_dms = _embed_queued_surface_components("rec_", recon_surface)
    extra_dms = list(recon_dms)

    # Filter out the old wrapper root & main column; reorganize children into clean tabs
    kept_recon = [
        c for c in recon_comps if c["id"] not in {"rec_root", "rec_canvas-main-col", "rec_canvas-div-top"}
    ]
    kept_ids = {c["id"] for c in kept_recon}

    tab1_children = [
        cid
        for cid in ("rec_canvas-subtitle", "rec_canvas-table", "rec_canvas-chart")
        if cid in kept_ids
    ]
    tab2_children = [
        cid
        for cid in ("rec_canvas-acc-1", "rec_canvas-acc-2", "rec_canvas-action-row")
        if cid in kept_ids
    ]

    tab_ids = ["rec_tab_spend", "rec_tab_actions"]
    tab_components: list[dict[str, Any]] = [
        {
            "id": "rec_tab_spend",
            "component": "MaterialTab",
            "label": "📊 Statement & Spend",
            "children": ["rec_tab1_col"],
        },
        {
            "id": "rec_tab1_col",
            "component": "MaterialColumn",
            "style": {"gap": "14px", "padding": "14px 4px"},
            "children": tab1_children,
        },
        {
            "id": "rec_tab_actions",
            "component": "MaterialTab",
            "label": "📋 Action Plan",
            "children": ["rec_tab2_col"],
        },
        {
            "id": "rec_tab2_col",
            "component": "MaterialColumn",
            "style": {"gap": "12px", "padding": "14px 4px"},
            "children": tab2_children,
        },
    ]

    if sn_surface is not None:
        _, sn_comps, sn_dms = _embed_queued_surface_components("recsn_", sn_surface)
        extra_dms.extend(sn_dms)
        kept_sn = [c for c in sn_comps if c["id"] != "recsn_root"]
        kept_recon.extend(kept_sn)
        tab_ids.append("rec_tab_handoff")
        tab_components.append(
            {
                "id": "rec_tab_handoff",
                "component": "MaterialTab",
                "label": "🎫 P1 ServiceNow Handoff",
                "children": ["recsn_sn-form-col"],
            }
        )

    title_comp = next((c for c in kept_recon if c["id"] == "rec_canvas-title"), None)
    if title_comp is not None:
        title_comp["usageHint"] = "h4"
    else:
        kept_recon.append(
            {
                "id": "rec_canvas-title",
                "component": "MaterialText",
                "text": "Multi-Invoice Statement Reconciliation",
                "usageHint": "h4",
                "style": {"color": BRAND_BLACK, "fontWeight": "600"},
            }
        )

    wrapper_components: list[dict[str, Any]] = [
        {
            "id": "rec_tabbed_card",
            "component": "MaterialCard",
            "appearance": "outlined",
            "style": {
                "backgroundColor": BRAND_WHITE,
                "border": f"1px solid {BRAND_BORDER}",
                "borderLeft": f"4px solid {BRAND_GREEN}",
                "borderRadius": "12px",
                "boxShadow": "0 2px 8px rgba(17, 17, 17, 0.06)",
            },
            "children": ["rec_tabbed_col"],
        },
        {
            "id": "rec_tabbed_col",
            "component": "MaterialColumn",
            "style": {"gap": "12px", "padding": "16px 18px"},
            "children": ["rec_tabbed_hdr", "rec_tabs"],
        },
        {
            "id": "rec_tabbed_hdr",
            "component": "MaterialRow",
            "align": "center",
            "style": {"gap": "10px", "flexWrap": "wrap"},
            "children": ["rec_hdr_badge", "rec_canvas-title", "rec_hdr_pill"],
        },
        {
            "id": "rec_hdr_badge",
            "component": "MaterialBadge",
            "text": "P1",
            "color": "warn",
            "size": "small",
            "overlap": False,
            "description": "P1 Legal Escalation Required",
            "children": ["rec_hdr_icon"],
        },
        {
            "id": "rec_hdr_icon",
            "component": "MaterialIcon",
            "icon": "table_chart",
            "style": {"color": BRAND_GREEN},
        },
        {
            "id": "rec_hdr_pill",
            "component": "MaterialText",
            "text": "P1 LEGAL HOLD • 3 INVOICES",
            "usageHint": "caption",
            "style": {
                "backgroundColor": BRAND_BLACK,
                "color": BRAND_WHITE,
                "padding": "4px 10px",
                "borderRadius": "999px",
                "fontWeight": "600",
                "fontSize": "11px",
                "letterSpacing": "0.4px",
                "marginLeft": "auto",
            },
        },
        {
            "id": "rec_tabs",
            "component": "MaterialTabs",
            "activeTab": 0,
            "ariaLabel": "Statement Reconciliation Panels",
            "children": tab_ids,
        },
        *tab_components,
        *kept_recon,
    ]
    return "rec_tabbed_card", wrapper_components, extra_dms


def _build_contextual_followup_chips(
    user_query: str,
    assistant_text: str,
    queued_surfaces: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Builds 3 contextual next-best-action `MaterialChips` options for prompted responses."""
    surface_ids = {str(s.get("surface_id") or "") for s in queued_surfaces}
    combined = f"{user_query} {assistant_text}"
    inv_match = _INVOICE_TOKEN_RE.search(combined)
    inv_token = inv_match.group(1) if inv_match else "INV-902605"

    if "gbs-missing-identifier-card" in surface_ids:
        return [
            {
                "label": "🔍 Sample: Check INV-902605",
                "value": "Check Coupa and Oracle status for INV-902605",
            },
            {
                "label": "📊 Reconcile BioData Statement",
                "value": "Reconcile BioData statement invoices 923469, 923104, and 923288",
            },
            {
                "label": "📄 Non-PO Guide (KB0010101)",
                "value": "Open Coupa policy guide KB0010101",
            },
        ]
    if "gbs-biodata-reconciliation-canvas" in surface_ids:
        return [
            {
                "label": "📄 Payment Terms Policy (KB0010108)",
                "value": "Open Coupa policy guide KB0010108",
            },
            {
                "label": "🔍 Audit Invoice 923288 Blocker",
                "value": "Explain how to resolve the 3-way match goods receipt blocker on invoice 923288",
            },
            {
                "label": "🔍 Check Another Invoice",
                "value": "Check Coupa and Oracle status for INV-902605",
            },
        ]
    if "gbs-servicenow-intake-form" in surface_ids or any(
        sid.startswith("gbs-servicenow-receipt-") for sid in surface_ids
    ):
        return [
            {
                "label": "🔍 Check Invoice INV-902605",
                "value": "Check Coupa and Oracle status for INV-902605",
            },
            {
                "label": "📄 AP Hold Policy (KB0010108)",
                "value": "Open Coupa policy guide KB0010108",
            },
            {
                "label": "📘 Non-PO Coding (KB0010101)",
                "value": "Open Coupa policy guide KB0010101",
            },
        ]
    if inv_match is not None and not surface_ids:
        return [
            {
                "label": "📄 Net 30 Policy (KB0010108)",
                "value": "Open Coupa policy guide KB0010108",
            },
            {
                "label": f"⚡ Urgent EFT ({inv_token})",
                "value": f"Request urgent early payment for {inv_token}",
            },
            {
                "label": f"🎫 Escalate {inv_token} to AP",
                "value": f"Open a ServiceNow support case for {inv_token}",
            },
        ]
    return [
        {
            "label": "🔍 Check Invoice INV-902605",
            "value": "Check Coupa and Oracle status for INV-902605",
        },
        {
            "label": "📄 Non-PO Coding (KB0010101)",
            "value": "Open Coupa policy guide KB0010101",
        },
        {
            "label": "📊 Reconcile BioData Statement",
            "value": "Reconcile BioData statement invoices 923469, 923104, and 923288",
        },
    ]


def build_unified_app_in_canvas_surface(
    *,
    user_query: str = "",
    assistant_text: str = "",
    pending_surfaces: Optional[list[dict[str, Any]]] = None,
    prior_turns: Optional[list[dict[str, str]]] = None,
    initial_launch_only: bool = False,
    auto_fullscreen: bool = True,
) -> dict[str, Any]:
    """Composes the App-in-Canvas A2UI v0.9 surface (`gbs-app-in-canvas`).

    - When `initial_launch_only=True`: Renders ONLY the `AskFinance Agent` card.
    - When `initial_launch_only=False`: Renders
      1. `ws-chat-card` (Text Response Card + Follow-up `MaterialChips`) FIRST,
      2. `ws-stage-col` (Prompted `MaterialCard`s / Form Inputs / Tabs, excluding
         `Cross-System Coupa & Oracle Tracker` cards) BELOW,
      3. `ws-composer-card` (`AskFinance Agent`) AT THE BOTTOM.
    """
    queued = filter_prompt_display_surfaces(pending_surfaces)
    history = list(prior_turns or [])
    icon_data_uri = get_helpdesk_icon_data_uri()

    composer_btn_children = ["ws-composer-send"]
    if not initial_launch_only:
        composer_btn_children.append("ws-composer-reset")

    fs_icon = "close_fullscreen" if auto_fullscreen else "open_in_full"
    fs_label = (
        "Switch to Split-Screen Side Panel"
        if auto_fullscreen
        else "Expand Canvas to Full Screen"
    )

    composer_components: list[dict[str, Any]] = [
        {
            "id": "ws-composer-card",
            "component": "MaterialCard",
            "appearance": "outlined",
            "style": {
                "backgroundColor": BRAND_WHITE,
                "border": f"1px solid {BRAND_BORDER}",
                "borderTop": f"4px solid {BRAND_GREEN}",
                "borderRadius": "12px",
                "boxShadow": "0 4px 14px rgba(17, 17, 17, 0.08)",
                "overflow": "hidden",
            },
            "children": ["ws-composer-shell"],
        },
        {
            "id": "ws-composer-shell",
            "component": "MaterialColumn",
            "style": {"gap": "0px"},
            "children": [
                "ws-composer-header",
                "ws-composer-body",
            ],
        },
        {
            "id": "ws-composer-header",
            "component": "MaterialRow",
            "align": "center",
            "style": {
                "backgroundColor": BRAND_BLACK,
                "color": BRAND_WHITE,
                "padding": "14px 18px",
                "gap": "12px",
            },
            "children": ["ws-composer-icon", "ws-composer-title-col", "ws-composer-fs-toggle"],
        },
        {
            "id": "ws-composer-icon",
            "component": "MaterialImage",
            "url": icon_data_uri,
            "alt": "AskFinance Agent Icon",
            "width": "38px",
            "height": "38px",
            "fit": "contain",
            "borderRadius": "8px",
        },
        {
            "id": "ws-composer-title-col",
            "component": "MaterialColumn",
            "style": {"gap": "2px"},
            "children": ["ws-composer-title", "ws-composer-subtitle"],
        },
        {
            "id": "ws-composer-title",
            "component": "MaterialText",
            "text": HELPDESK_CARD_TITLE,
            "usageHint": "h4",
            "style": {
                "color": BRAND_WHITE,
                "fontWeight": "600",
                "margin": "0",
            },
        },
        {
            "id": "ws-composer-subtitle",
            "component": "MaterialText",
            "text": "Enterprise Finance & Procurement AI Support",
            "usageHint": "caption",
            "style": {
                "color": BRAND_DIVIDER,
                "letterSpacing": "0.3px",
                "margin": "0",
            },
        },
        {
            "id": "ws-composer-fs-toggle",
            "component": "MaterialIconButton",
            "icon": fs_icon,
            "ariaLabel": fs_label,
            "tooltip": fs_label,
            "style": {
                "color": BRAND_WHITE,
                "marginLeft": "auto",
            },
            "action": {
                "event": {
                    "name": "toggle_canvas_fullscreen",
                    "context": {
                        "fullscreen": not auto_fullscreen,
                    },
                }
            },
        },
        {
            "id": "ws-composer-body",
            "component": "MaterialColumn",
            "style": {
                "backgroundColor": BRAND_WHITE,
                "gap": "12px",
                "padding": "18px",
            },
            "children": [
                "ws-composer-input",
                "ws-composer-btn-row",
            ],
        },
        {
            "id": "ws-composer-input",
            "component": "MaterialInput",
            "label": "Ask a question, enter an Invoice/PO #, or request an escalation",
            "placeholder": "e.g., Check INV-902605, how do I code a Non-PO invoice, or escalate 902158...",
            "value": {"path": "/canvasChat/prompt"},
            "checks": [
                {
                    "condition": {
                        "call": "or",
                        "args": {
                            "values": [
                                {
                                    "call": "not",
                                    "args": {
                                        "value": {
                                            "call": "required",
                                            "args": {"value": {"path": "/canvasChat/prompt"}},
                                        }
                                    },
                                },
                                {
                                    "call": "regex",
                                    "args": {
                                        "value": {"path": "/canvasChat/prompt"},
                                        "pattern": ".*\\S.*\\S.*",
                                    },
                                },
                            ]
                        },
                    },
                    "message": "Please enter at least 2 characters (e.g., an Invoice #, PO #, or question).",
                }
            ],
        },
        {
            "id": "ws-composer-btn-row",
            "component": "MaterialRow",
            "align": "center",
            "style": {"gap": "10px", "flexWrap": "wrap"},
            "children": composer_btn_children,
        },
        {
            "id": "ws-composer-send",
            "component": "MaterialButton",
            "label": "Send",
            "variant": "raised",
            "color": "primary",
            "leadingIcon": "send",
            "style": {
                "backgroundColor": BRAND_GREEN,
                "color": BRAND_WHITE,
                "fontWeight": "600",
                "borderRadius": "6px",
            },
            "action": {
                "event": {
                    "name": "submit_canvas_chat_prompt",
                    "context": {
                        "prompt": {"path": "/canvasChat/prompt"},
                    },
                }
            },
        },
        *(
            [
                {
                    "id": "ws-composer-reset",
                    "component": "MaterialButton",
                    "label": "Clear View",
                    "variant": "stroked",
                    "leadingIcon": "refresh",
                    "style": {
                        "borderColor": BRAND_BLACK,
                        "color": BRAND_BLACK,
                        "fontWeight": "500",
                        "borderRadius": "6px",
                    },
                    "action": {
                        "event": {
                            "name": "canvas_nav_home",
                            "context": {"action": "reset"},
                        }
                    },
                }
            ]
            if not initial_launch_only
            else []
        ),
    ]

    main_children: list[str] = []
    prompted_components: list[dict[str, Any]] = []
    extra_data_models: list[dict[str, Any]] = []

    if initial_launch_only:
        stage_summary = "Enterprise Finance & Procurement AI Support"
        main_children.append("ws-composer-card")
    else:
        clean_user = sanitize_display_text(user_query) or "Helpdesk Inquiry"
        fallback_assistant = (
            "Please review or complete the interactive card below."
            if queued
            else "How else can AskFinance Agent assist you today?"
        )
        clean_assistant = sanitize_display_text(assistant_text) or fallback_assistant

        # Build collapsible sanitized prior turns
        history_panel_ids: list[str] = []
        history_components: list[dict[str, Any]] = []
        recent_prior = history[-MAX_HISTORY_TURNS:]
        panel_num = 0
        for turn in reversed(recent_prior):
            prev_u = sanitize_display_text(str(turn.get("user") or ""))
            prev_a = sanitize_display_text(str(turn.get("assistant") or ""))
            if not prev_u and not prev_a:
                continue
            panel_num += 1
            p_id = f"ws-hist-panel-{panel_num}"
            b_id = f"ws-hist-body-{panel_num}"
            history_panel_ids.append(p_id)
            history_components.append(
                {
                    "id": p_id,
                    "component": "MaterialExpansionPanel",
                    "title": _clamp_title(f"Earlier Turn: {prev_u or 'Inquiry'}"),
                    "expanded": False,
                    "style": {
                        "backgroundColor": BRAND_CANVAS_BG,
                        "border": f"1px solid {BRAND_DIVIDER}",
                        "borderRadius": "8px",
                    },
                    "children": [b_id],
                }
            )
            history_components.append(
                {
                    "id": b_id,
                    "component": "MaterialText",
                    "text": f"**Inquiry:** {prev_u or 'Inquiry'}\n\n**AskFinance Agent:** {prev_a}",
                    "usageHint": "body2",
                    "style": {"color": BRAND_CHARCOAL},
                }
            )

        chip_options = _build_contextual_followup_chips(clean_user, clean_assistant, queued)

        chat_col_children = [
            "ws-latest-user-row",
            "ws-chat-div",
            "ws-latest-assistant",
            "ws-followup-row",
        ]
        if history_panel_ids:
            chat_col_children.extend(["ws-hist-div", *history_panel_ids])

        # 1. FIRST: Agent Text Response Card (`ws-chat-card`)
        main_children.append("ws-chat-card")
        prompted_components.extend(
            [
                {
                    "id": "ws-chat-card",
                    "component": "MaterialCard",
                    "appearance": "outlined",
                    "style": {
                        "backgroundColor": BRAND_WHITE,
                        "border": f"1px solid {BRAND_BORDER}",
                        "borderLeft": f"4px solid {BRAND_GREEN}",
                        "borderRadius": "12px",
                        "boxShadow": "0 2px 8px rgba(17, 17, 17, 0.05)",
                    },
                    "children": ["ws-chat-col"],
                },
                {
                    "id": "ws-chat-col",
                    "component": "MaterialColumn",
                    "style": {"gap": "12px", "padding": "16px 18px"},
                    "children": chat_col_children,
                },
                {
                    "id": "ws-latest-user-row",
                    "component": "MaterialRow",
                    "align": "center",
                    "style": {"gap": "8px", "flexWrap": "wrap"},
                    "children": ["ws-latest-user-icon", "ws-latest-user", "ws-response-pill"],
                },
                {
                    "id": "ws-latest-user-icon",
                    "component": "MaterialIcon",
                    "icon": "support_agent",
                    "style": {"color": BRAND_GREEN},
                },
                {
                    "id": "ws-latest-user",
                    "component": "MaterialText",
                    "text": f"**Inquiry:** {clean_user}",
                    "usageHint": "body2",
                    "style": {"color": BRAND_BLACK, "fontWeight": "600"},
                },
                {
                    "id": "ws-response-pill",
                    "component": "MaterialText",
                    "text": "✓ RESPONSE READY",
                    "usageHint": "caption",
                    "style": {
                        "backgroundColor": BRAND_GREEN_TINT,
                        "color": BRAND_GREEN,
                        "border": f"1px solid {BRAND_BORDER}",
                        "padding": "3px 10px",
                        "borderRadius": "999px",
                        "fontWeight": "600",
                        "fontSize": "11px",
                        "letterSpacing": "0.4px",
                        "marginLeft": "auto",
                    },
                },
                {
                    "id": "ws-chat-div",
                    "component": "MaterialDivider",
                    "style": {"borderColor": BRAND_DIVIDER},
                },
                {
                    "id": "ws-latest-assistant",
                    "component": "MaterialText",
                    "text": clean_assistant,
                    "usageHint": "body1",
                    "style": {"color": BRAND_BLACK, "lineHeight": "1.55"},
                },
                {
                    "id": "ws-followup-row",
                    "component": "MaterialColumn",
                    "style": {"gap": "6px", "paddingTop": "4px"},
                    "children": ["ws-followup-label", "ws-followup-chips"],
                },
                {
                    "id": "ws-followup-label",
                    "component": "MaterialText",
                    "text": "Suggested Next Actions:",
                    "usageHint": "caption",
                    "style": {"color": BRAND_CHARCOAL, "fontWeight": "600"},
                },
                {
                    "id": "ws-followup-chips",
                    "component": "MaterialChips",
                    "ariaLabel": "Suggested follow-up actions",
                    "value": {"path": "/canvasChat/selectedChip"},
                    "options": chip_options,
                    "action": {
                        "event": {
                            "name": "select_followup_chip",
                            "context": {
                                "chipPrompt": {"path": "/canvasChat/selectedChip"},
                            },
                        }
                    },
                },
                *(
                    [
                        {
                            "id": "ws-hist-div",
                            "component": "MaterialDivider",
                            "style": {"borderColor": BRAND_DIVIDER},
                        },
                        *history_components,
                    ]
                    if history_panel_ids
                    else []
                ),
            ]
        )

        # 2. SECOND (BELOW Text Response Card): Prompted Interactive MaterialCards / Forms / Tabs
        stage_child_ids: list[str] = []
        stage_components: list[dict[str, Any]] = []

        recon_surf = next(
            (s for s in queued if s.get("surface_id") == "gbs-biodata-reconciliation-canvas"),
            None,
        )
        sn_surf = (
            next(
                (s for s in queued if s.get("surface_id") == "gbs-servicenow-intake-form"),
                None,
            )
            if recon_surf is not None
            else None
        )

        consumed_ids: set[int] = set()
        if recon_surf is not None:
            consumed_ids.add(id(recon_surf))
            if sn_surf is not None:
                consumed_ids.add(id(sn_surf))
            tab_root_id, tab_comps, tab_dms = _build_tabbed_reconciliation_card(recon_surf, sn_surf)
            stage_child_ids.append(tab_root_id)
            stage_components.extend(tab_comps)
            extra_data_models.extend(tab_dms)

        for idx, surf in enumerate(queued):
            if id(surf) in consumed_ids:
                continue
            root_id, comps, dms = _embed_queued_surface_components(f"stg{idx}_", surf)
            if comps:
                stage_child_ids.append(root_id)
                stage_components.extend(comps)
                extra_data_models.extend(dms)

        if stage_child_ids:
            main_children.append("ws-stage-col")
            prompted_components.append(
                {
                    "id": "ws-stage-col",
                    "component": "MaterialColumn",
                    "style": {"gap": "14px"},
                    "children": stage_child_ids,
                }
            )
            prompted_components.extend(stage_components)
            stage_summary = " • ".join(
                str(s.get("readable_text") or s.get("surface_id") or "Active Workflow")
                for s in queued
            )
        else:
            stage_summary = clean_user[:120]

        # 3. THIRD (Bottom): Persistent AskFinance Agent composer card
        main_children.append("ws-composer-card")

    components: list[dict[str, Any]] = [
        {
            "id": "root",
            "component": "Canvas",
            "autoOpen": True,
            "autoFullscreen": auto_fullscreen,
            "cardTitle": HELPDESK_CARD_TITLE,
            "cardDescription": stage_summary[:160],
            "cardIcon": "support_agent",
            "children": ["ws-main-col"],
        },
        {
            "id": "ws-main-col",
            "component": "MaterialColumn",
            "style": {
                "backgroundColor": BRAND_CANVAS_BG,
                "gap": "16px",
                "padding": "20px",
                "borderRadius": "12px",
            },
            "children": main_children,
        },
        *prompted_components,
        *composer_components,
    ]

    deduped_dms: dict[str, dict[str, Any]] = {}
    for dm_msg in extra_data_models:
        p = str(dm_msg["updateDataModel"].get("path") or "/")
        deduped_dms[p] = dm_msg
    deduped_dms["/canvasChat"] = {
        "version": A2UI_VERSION,
        "updateDataModel": {
            "surfaceId": APP_CANVAS_SURFACE_ID,
            "path": "/canvasChat",
            "value": {"prompt": "", "selectedChip": ""},
        },
    }

    messages: list[dict[str, Any]] = [
        {
            "version": A2UI_VERSION,
            "createSurface": {
                "surfaceId": APP_CANVAS_SURFACE_ID,
                "catalogId": GE_COMPOSITE_CATALOG_ID,
                "sendDataModel": True,
                "theme": {
                    "primaryColor": BRAND_GREEN,
                    "agentDisplayName": HELPDESK_CARD_TITLE,
                },
            },
        },
        {
            "version": A2UI_VERSION,
            "updateComponents": {
                "surfaceId": APP_CANVAS_SURFACE_ID,
                "components": components,
            },
        },
        *deduped_dms.values(),
    ]
    readable = f"{HELPDESK_CARD_TITLE}: {stage_summary[:120]}"
    return package_a2ui_datapart(
        messages,
        surface_id=APP_CANVAS_SURFACE_ID,
        readable_text=readable,
    )


def build_working_status_app_in_canvas_surface(
    *,
    user_query: str = "",
    stage_headline: str = "Working on your request...",
    stage_detail: str = (
        "AskFinance Agent received your prompt and is querying Coupa, Oracle Financials, and ServiceNow..."
    ),
    prior_turns: Optional[list[dict[str, str]]] = None,
    auto_fullscreen: bool = True,
) -> dict[str, Any]:
    """Builds an immediate A2UI v0.9 streaming acknowledgement surface (`gbs-app-in-canvas`).

    Streamed to Gemini Enterprise within milliseconds of clicking **Send** so the user
    immediately sees an animated Green `MaterialProgressSpinner` + indeterminate
    `MaterialProgressBar`, confirmation of their submitted prompt, and a disabled
    `Working...` button while the multi-agent backend processes the turn.
    """
    clean_user = sanitize_display_text(user_query) or "Processing inquiry..."
    history = list(prior_turns or [])
    icon_data_uri = get_helpdesk_icon_data_uri()

    fs_icon = "close_fullscreen" if auto_fullscreen else "open_in_full"
    fs_label = (
        "Switch to Split-Screen Side Panel"
        if auto_fullscreen
        else "Expand Canvas to Full Screen"
    )

    history_panel_ids: list[str] = []
    history_components: list[dict[str, Any]] = []
    panel_num = 0
    for turn in reversed(history[-MAX_HISTORY_TURNS:]):
        prev_u = sanitize_display_text(str(turn.get("user") or ""))
        prev_a = sanitize_display_text(str(turn.get("assistant") or ""))
        if not prev_u and not prev_a:
            continue
        panel_num += 1
        p_id = f"ws-hist-panel-{panel_num}"
        b_id = f"ws-hist-body-{panel_num}"
        history_panel_ids.append(p_id)
        history_components.append(
            {
                "id": p_id,
                "component": "MaterialExpansionPanel",
                "title": _clamp_title(f"Earlier Turn: {prev_u or 'Inquiry'}"),
                "expanded": False,
                "style": {
                    "backgroundColor": BRAND_CANVAS_BG,
                    "border": f"1px solid {BRAND_DIVIDER}",
                    "borderRadius": "8px",
                },
                "children": [b_id],
            }
        )
        history_components.append(
            {
                "id": b_id,
                "component": "MaterialText",
                "text": f"**Inquiry:** {prev_u or 'Inquiry'}\n\n**AskFinance Agent:** {prev_a}",
                "usageHint": "body2",
                "style": {"color": BRAND_CHARCOAL},
            }
        )

    working_col_children = [
        "ws-working-hdr",
        "ws-working-bar",
        "ws-working-user",
        "ws-working-detail",
    ]
    if history_panel_ids:
        working_col_children.extend(["ws-working-hist-div", *history_panel_ids])

    components: list[dict[str, Any]] = [
        {
            "id": "root",
            "component": "Canvas",
            "autoOpen": True,
            "autoFullscreen": auto_fullscreen,
            "cardTitle": HELPDESK_CARD_TITLE,
            "cardDescription": f"Working: {clean_user[:120]}",
            "cardIcon": "support_agent",
            "children": ["ws-main-col"],
        },
        {
            "id": "ws-main-col",
            "component": "MaterialColumn",
            "style": {
                "backgroundColor": BRAND_CANVAS_BG,
                "gap": "16px",
                "padding": "20px",
                "borderRadius": "12px",
            },
            "children": ["ws-working-card", "ws-composer-card"],
        },
        {
            "id": "ws-working-card",
            "component": "MaterialCard",
            "appearance": "outlined",
            "style": {
                "backgroundColor": BRAND_WHITE,
                "border": f"1px solid {BRAND_BORDER}",
                "borderLeft": f"4px solid {BRAND_GREEN}",
                "borderRadius": "12px",
                "boxShadow": "0 2px 10px rgba(0, 135, 104, 0.10)",
            },
            "children": ["ws-working-col"],
        },
        {
            "id": "ws-working-col",
            "component": "MaterialColumn",
            "style": {"gap": "12px", "padding": "16px 18px"},
            "children": working_col_children,
        },
        {
            "id": "ws-working-hdr",
            "component": "MaterialRow",
            "align": "center",
            "style": {"gap": "10px", "flexWrap": "wrap"},
            "children": ["ws-working-spinner", "ws-working-title", "ws-working-pill"],
        },
        {
            "id": "ws-working-spinner",
            "component": "MaterialProgressSpinner",
            "mode": "indeterminate",
            "diameter": 22,
            "strokeWidth": 3,
            "color": "primary",
            "ariaLabel": "AskFinance Agent processing request",
        },
        {
            "id": "ws-working-title",
            "component": "MaterialText",
            "text": stage_headline,
            "usageHint": "h4",
            "style": {"color": BRAND_BLACK, "fontWeight": "600", "margin": "0"},
        },
        {
            "id": "ws-working-pill",
            "component": "MaterialText",
            "text": "WORKING • LIVE",
            "usageHint": "caption",
            "style": {
                "backgroundColor": BRAND_GREEN,
                "color": BRAND_WHITE,
                "padding": "4px 10px",
                "borderRadius": "999px",
                "fontWeight": "600",
                "fontSize": "11px",
                "letterSpacing": "0.4px",
                "marginLeft": "auto",
            },
        },
        {
            "id": "ws-working-bar",
            "component": "MaterialProgressBar",
            "mode": "indeterminate",
            "color": "primary",
            "ariaLabel": "Generating response and interactive UI card",
        },
        {
            "id": "ws-working-user",
            "component": "MaterialText",
            "text": f"**Prompt Received:** {clean_user}",
            "usageHint": "body2",
            "style": {"color": BRAND_BLACK, "fontWeight": "600"},
        },
        {
            "id": "ws-working-detail",
            "component": "MaterialText",
            "text": stage_detail,
            "usageHint": "body2",
            "style": {"color": BRAND_CHARCOAL},
        },
        *(
            [
                {
                    "id": "ws-working-hist-div",
                    "component": "MaterialDivider",
                    "style": {"borderColor": BRAND_DIVIDER},
                },
                *history_components,
            ]
            if history_panel_ids
            else []
        ),
        {
            "id": "ws-composer-card",
            "component": "MaterialCard",
            "appearance": "outlined",
            "style": {
                "backgroundColor": BRAND_WHITE,
                "border": f"1px solid {BRAND_BORDER}",
                "borderTop": f"4px solid {BRAND_GREEN}",
                "borderRadius": "12px",
                "boxShadow": "0 4px 14px rgba(17, 17, 17, 0.08)",
                "overflow": "hidden",
            },
            "children": ["ws-composer-shell"],
        },
        {
            "id": "ws-composer-shell",
            "component": "MaterialColumn",
            "style": {"gap": "0px"},
            "children": ["ws-composer-header", "ws-composer-body"],
        },
        {
            "id": "ws-composer-header",
            "component": "MaterialRow",
            "align": "center",
            "style": {
                "backgroundColor": BRAND_BLACK,
                "color": BRAND_WHITE,
                "padding": "14px 18px",
                "gap": "12px",
            },
            "children": ["ws-composer-icon", "ws-composer-title-col", "ws-composer-fs-toggle"],
        },
        {
            "id": "ws-composer-icon",
            "component": "MaterialImage",
            "url": icon_data_uri,
            "alt": "AskFinance Agent Icon",
            "width": "38px",
            "height": "38px",
            "fit": "contain",
            "borderRadius": "8px",
        },
        {
            "id": "ws-composer-title-col",
            "component": "MaterialColumn",
            "style": {"gap": "2px"},
            "children": ["ws-composer-title", "ws-composer-subtitle"],
        },
        {
            "id": "ws-composer-title",
            "component": "MaterialText",
            "text": HELPDESK_CARD_TITLE,
            "usageHint": "h4",
            "style": {"color": BRAND_WHITE, "fontWeight": "600", "margin": "0"},
        },
        {
            "id": "ws-composer-subtitle",
            "component": "MaterialText",
            "text": "Enterprise Finance & Procurement AI Support",
            "usageHint": "caption",
            "style": {"color": BRAND_DIVIDER, "letterSpacing": "0.3px", "margin": "0"},
        },
        {
            "id": "ws-composer-fs-toggle",
            "component": "MaterialIconButton",
            "icon": fs_icon,
            "ariaLabel": fs_label,
            "tooltip": fs_label,
            "disabled": True,
            "style": {"color": BRAND_WHITE, "marginLeft": "auto"},
            "action": {
                "event": {
                    "name": "toggle_canvas_fullscreen",
                    "context": {"fullscreen": not auto_fullscreen},
                }
            },
        },
        {
            "id": "ws-composer-body",
            "component": "MaterialColumn",
            "style": {"backgroundColor": BRAND_WHITE, "gap": "12px", "padding": "18px"},
            "children": ["ws-composer-input", "ws-composer-btn-row"],
        },
        {
            "id": "ws-composer-input",
            "component": "MaterialInput",
            "label": "Agent is processing your prompt...",
            "placeholder": "Please wait while AskFinance Agent generates your response and interactive card...",
            "disabled": True,
            "value": {"path": "/canvasChat/prompt"},
        },
        {
            "id": "ws-composer-btn-row",
            "component": "MaterialRow",
            "align": "center",
            "style": {"gap": "10px", "flexWrap": "wrap"},
            "children": ["ws-composer-send"],
        },
        {
            "id": "ws-composer-send",
            "component": "MaterialButton",
            "label": "Working...",
            "variant": "raised",
            "color": "primary",
            "disabled": True,
            "leadingIcon": "hourglass_top",
            "style": {
                "backgroundColor": BRAND_CHARCOAL,
                "color": BRAND_WHITE,
                "fontWeight": "600",
                "borderRadius": "6px",
            },
            "action": {
                "event": {
                    "name": "submit_canvas_chat_prompt",
                    "context": {"prompt": {"path": "/canvasChat/prompt"}},
                }
            },
        },
    ]

    messages: list[dict[str, Any]] = [
        {
            "version": A2UI_VERSION,
            "createSurface": {
                "surfaceId": APP_CANVAS_SURFACE_ID,
                "catalogId": GE_COMPOSITE_CATALOG_ID,
                "sendDataModel": True,
                "theme": {
                    "primaryColor": BRAND_GREEN,
                    "agentDisplayName": HELPDESK_CARD_TITLE,
                },
            },
        },
        {
            "version": A2UI_VERSION,
            "updateComponents": {
                "surfaceId": APP_CANVAS_SURFACE_ID,
                "components": components,
            },
        },
        {
            "version": A2UI_VERSION,
            "updateDataModel": {
                "surfaceId": APP_CANVAS_SURFACE_ID,
                "path": "/canvasChat",
                "value": {"prompt": "", "selectedChip": ""},
            },
        },
    ]
    readable = f"{HELPDESK_CARD_TITLE} (Working): {clean_user[:110]}"
    return package_a2ui_datapart(
        messages,
        surface_id=APP_CANVAS_SURFACE_ID,
        readable_text=readable,
    )


def attach_app_in_canvas_to_response(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
    auto_fullscreen: bool = True,
) -> None:
    """Idempotently attaches the unified `gbs-app-in-canvas` surface to a final model turn."""
    try:
        if getattr(llm_response, "partial", False):
            return

        # Hold intermediate step 1 of SequentialAgent so step 2 emits one combined Canvas
        agent_name = getattr(callback_context, "agent_name", "")
        if agent_name == "statement_reconciliation_step_agent":
            return

        content = getattr(llm_response, "content", None)
        parts = list(getattr(content, "parts", None) or []) if content else []

        has_function_call = any(getattr(p, "function_call", None) is not None for p in parts)
        if has_function_call:
            return

        existing_surfaces = {
            (getattr(p, "part_metadata", None) or {}).get("surface_id")
            for p in parts
            if getattr(p, "part_metadata", None)
        }
        if APP_CANVAS_SURFACE_ID in existing_surfaces:
            return

        state = callback_context.state
        effective_fs = (
            bool(state.get("canvas_auto_fullscreen", auto_fullscreen))
            if state is not None
            else auto_fullscreen
        )
        initial_launch_only = bool(
            state is not None and state.get("canvas_initial_launch_only") is True
        )
        pending = (
            filter_prompt_display_surfaces(list(state.get("pending_a2ui_parts") or []))
            if state is not None
            else []
        )
        # Ensure no raw standalone gbs-invoice-status-* DataPart ever remains on parts
        parts = [
            p
            for p in parts
            if not str((getattr(p, "part_metadata", None) or {}).get("surface_id") or "").startswith(
                "gbs-invoice-status-"
            )
        ]
        prior_turns = list(state.get("canvas_chat_history") or []) if state is not None else []
        raw_user_query = (
            str(state.get("canvas_display_user_query") or state.get("last_user_query") or "")
            if state is not None
            else ""
        )
        user_query = sanitize_display_text(raw_user_query)

        # Scrub any accidental ADK transfer blocks from outgoing model text parts as well
        assistant_chunks: list[str] = []
        for p in parts:
            txt = getattr(p, "text", None)
            if txt:
                cleaned_part_txt = sanitize_display_text(str(txt))
                p.text = cleaned_part_txt
                if cleaned_part_txt:
                    assistant_chunks.append(cleaned_part_txt)
        assistant_text = "\n\n".join(assistant_chunks).strip()

        unified_pkg = build_unified_app_in_canvas_surface(
            user_query=user_query,
            assistant_text=assistant_text,
            pending_surfaces=[] if initial_launch_only else pending,
            prior_turns=[] if initial_launch_only else prior_turns,
            initial_launch_only=initial_launch_only,
            auto_fullscreen=effective_fs,
        )
        parts.extend(build_genai_a2ui_parts(unified_pkg))

        if content is None:
            llm_response.content = types.Content(role="model", parts=parts)
        else:
            content.parts = parts

        if state is not None:
            if not initial_launch_only:
                state["last_rendered_pending_surfaces"] = copy.deepcopy(pending)
                state["last_rendered_user_query"] = user_query
                state["last_rendered_assistant_text"] = assistant_text
            state["pending_a2ui_parts"] = []
            state["last_a2ui_surface_id"] = APP_CANVAS_SURFACE_ID
            state["canvas_initial_launch_only"] = False
            if not initial_launch_only and (user_query or assistant_text):
                sanitized_prior = [
                    {
                        "user": sanitize_display_text(str(t.get("user") or "")),
                        "assistant": sanitize_display_text(str(t.get("assistant") or "")),
                    }
                    for t in prior_turns
                    if sanitize_display_text(str(t.get("user") or ""))
                    or sanitize_display_text(str(t.get("assistant") or ""))
                ]
                updated_history = [
                    *sanitized_prior,
                    {
                        "user": user_query or "Inquiry",
                        "assistant": assistant_text[:600] or "Updated interactive workspace.",
                    },
                ]
                state["canvas_chat_history"] = updated_history[-MAX_HISTORY_TURNS:]

        _LOGGER.info(
            "AppInCanvasPlugin rendered AskFinance Agent Canvas",
            initial_launch_only=initial_launch_only,
            auto_fullscreen=effective_fs,
            embedded_surfaces=[p.get("surface_id") for p in pending],
        )
    except Exception as exc:
        _LOGGER.error(
            "AppInCanvasPlugin caught serialization exception safely",
            error_type=type(exc).__name__,
        )


class AppInCanvasPlugin(BasePlugin):
    """ADK `BasePlugin` ensuring EVERY completed turn renders inside `gbs-app-in-canvas`."""

    def __init__(
        self,
        name: str = "app_in_canvas_plugin",
        auto_fullscreen: bool = True,
    ) -> None:
        super().__init__(name=name)
        self.auto_fullscreen = auto_fullscreen

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> Optional[LlmResponse]:
        """Wraps response card + prompted tool surfaces + `AskFinance Agent` into one `Canvas`."""
        attach_app_in_canvas_to_response(
            callback_context=callback_context,
            llm_response=llm_response,
            auto_fullscreen=self.auto_fullscreen,
        )
        return None
