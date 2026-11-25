#!/usr/bin/env bash
set -euo pipefail

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-your-gcp-project-id}"
GE_LOCATION="${GEMINI_ENTERPRISE_LOCATION:-global}"
GE_APP_ID="${GEMINI_ENTERPRISE_APP_ID:-ask-finance-enterprise-app}"
AGENT_ENDPOINT_URI="${AGENT_ENDPOINT_URI:-}"

if [[ -z "${AGENT_ENDPOINT_URI}" ]]; then
  echo "Usage: AGENT_ENDPOINT_URI=https://ask-finance-agent-...a.run.app ./register_gemini_enterprise.sh"
  exit 1
fi

A2A_RPC_URL="${AGENT_ENDPOINT_URI%/}/a2a/ask_finance_agent"
ACCESS_TOKEN="$(gcloud auth print-access-token)"
API_BASE="https://discoveryengine.googleapis.com/v1alpha"
PARENT="projects/${PROJECT_ID}/locations/${GE_LOCATION}/collections/default_collection/engines/${GE_APP_ID}/assistants/default_assistant"

PAYLOAD="$(A2A_RPC_URL="${A2A_RPC_URL}" python3 - <<'PY'
import json
import os
from pathlib import Path

rpc_url = os.environ["A2A_RPC_URL"]
card_path = Path(__file__).resolve().parent / "ask_finance_agent" / "agent.json" if "__file__" in globals() else Path("ask_finance_agent/agent.json")
card = json.loads(card_path.read_text(encoding="utf-8"))
card["url"] = rpc_url
if isinstance(card.get("supportedInterfaces"), list) and card["supportedInterfaces"]:
    card["supportedInterfaces"][0]["url"] = rpc_url

body = {
    "displayName": "AskFinance Agent",
    "description": (
        "Multi-agent ADK assistant (Gemini 4 Argon + Gemini 2.5 Flash) for Supplier Invoice Support, "
        "Payment Diagnostics, Coupa KB Guidance, and 2-step HITL ServiceNow Case Creation with A2UI v0.9 Canvas."
    ),
    "a2aAgentDefinition": {
        "jsonAgentCard": json.dumps(card)
    },
}
print(json.dumps(body))
PY
)"

echo "==> Registering AskFinance Agent (${A2A_RPC_URL}) in Gemini Enterprise (${PARENT})..."

curl -fsS -X POST "${API_BASE}/${PARENT}/agents" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  -d "${PAYLOAD}"

echo ""
echo "==> Registration request submitted to Gemini Enterprise!"
