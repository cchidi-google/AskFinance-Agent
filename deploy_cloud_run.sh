#!/usr/bin/env bash
set -euo pipefail

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-your-gcp-project-id}"
CLOUD_RUN_REGION="${CLOUD_RUN_REGION:-us-central1}"
VERTEX_LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"
FLASH_MODEL="${GEMINI_FLASH_MODEL:-gemini-3.8-flash}"
PRO_MODEL="${GEMINI_PRO_MODEL:-gemini-4-argon}"
SERVICE_NAME="${SERVICE_NAME:-ask-finance-agent}"

echo "==> Deploying AskFinance Agent ADK + A2A Server (Dockerfile / main.py) to Cloud Run in ${PROJECT_ID} (${CLOUD_RUN_REGION})..."
echo "==> Vertex AI Endpoint: ${VERTEX_LOCATION} (Flash=${FLASH_MODEL}, Pro=${PRO_MODEL})"

gcloud run deploy "${SERVICE_NAME}" \
  --source="." \
  --project="${PROJECT_ID}" \
  --region="${CLOUD_RUN_REGION}" \
  --min-instances=1 \
  --max-instances=10 \
  --cpu=2 \
  --memory=2Gi \
  --timeout=300 \
  --allow-unauthenticated \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION},CLOUD_RUN_REGION=${CLOUD_RUN_REGION},GEMINI_FLASH_MODEL=${FLASH_MODEL},GEMINI_PRO_MODEL=${PRO_MODEL},GOOGLE_GENAI_USE_VERTEXAI=TRUE,ADK_ENABLE_A2A=true,ADK_ENABLE_WEB_UI=true,ENABLE_CLOUD_LOGGING=true,ENABLE_CLOUD_TRACE=true,ENABLE_SECRET_MANAGER=true"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${CLOUD_RUN_REGION}" --format='value(status.url)')"
echo "==> Deployed Service URL: ${SERVICE_URL}"
echo "==> A2A Endpoint URL:     ${SERVICE_URL}/a2a/ask_finance_agent"

gcloud run services update "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${CLOUD_RUN_REGION}" \
  --update-env-vars="PUBLIC_SERVICE_URL=${SERVICE_URL}" >/dev/null

ID_TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
echo "==> Pre-warming /warm endpoint..."
if [[ -n "${ID_TOKEN}" ]]; then
  curl -fsS -X POST -H "Authorization: Bearer ${ID_TOKEN}" "${SERVICE_URL}/warm" || true
else
  curl -fsS -X POST "${SERVICE_URL}/warm" || true
fi
echo ""
echo "==> Deployment complete!"
