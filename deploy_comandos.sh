set -euo pipefail

# --- 1) COMPLETAR ESTOS VALORES --------------------------------------------
PROJECT_ID="su-proyecto-gcp"            # ID del proyecto del cliente
REGION="southamerica-west1"             # misma región del dataset si se puede
EVENT_SET_ID="EL_EVENT_SET_ID"          # CRM Event Set ID de TikTok
SECRET_NAME="tiktok-access-token"       # nombre libre del secreto
SERVICE_NAME="tiktok-crm-events"        # nombre del servicio Cloud Run
SCHEDULE="17 * * * *"                   # cron (ej. cada hora en el minuto 17)
# ---------------------------------------------------------------------------

SA_NAME="tiktok-crm-run"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"

# --- 2) Habilitar APIs ------------------------------------------------------
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  bigquery.googleapis.com

# --- 3) Cuenta de servicio del pipeline ------------------------------------
gcloud iam service-accounts create "$SA_NAME" \
  --display-name "TikTok CRM Events pipeline" || true

# Roles de BigQuery (ajuste el dataset si el de control es distinto):
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SA}" --role "roles/bigquery.jobUser"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SA}" --role "roles/bigquery.dataEditor"
# (dataViewer sobre el dataset de leads: si está en otro dataset, concédalo
#  a nivel dataset. dataEditor de arriba cubre la tabla de control.)

# --- 4) Guardar el Access Token en Secret Manager --------------------------
# Pegue el token cuando lo pida (o use --data-file=- con echo -n).
printf '%s' "PEGUE_AQUI_EL_ACCESS_TOKEN" | \
  gcloud secrets create "$SECRET_NAME" --data-file=- || \
  printf '%s' "PEGUE_AQUI_EL_ACCESS_TOKEN" | \
  gcloud secrets versions add "$SECRET_NAME" --data-file=-

# Permitir que la cuenta de servicio lea el secreto:
gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
  --member "serviceAccount:${SA}" \
  --role "roles/secretmanager.secretAccessor"

# --- 5) Desplegar el servicio (privado) ------------------------------------
# Ejecutar dentro de la carpeta del proyecto (donde está el Dockerfile).
gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --region "$REGION" \
  --no-allow-unauthenticated \
  --service-account "$SA" \
  --timeout 3600 \
  --set-env-vars "CONFIG_PATH=config.yaml,TIKTOK_EVENT_SET_ID=${EVENT_SET_ID}" \
  --set-secrets "TIKTOK_ACCESS_TOKEN=${SECRET_NAME}:latest"

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" --format 'value(status.url)')"

# Permitir que el Scheduler invoque el servicio:
gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --region "$REGION" \
  --member "serviceAccount:${SA}" \
  --role "roles/run.invoker"

# --- 6) Programar la ejecución ---------------------------------------------
gcloud scheduler jobs create http "${SERVICE_NAME}-schedule" \
  --schedule "$SCHEDULE" \
  --uri "$SERVICE_URL" \
  --http-method POST \
  --oidc-service-account-email "$SA" \
  --attempt-deadline 1800s \
  --location "$REGION"

echo "Listo. Servicio: $SERVICE_URL"
echo "Prueba manual:  curl -X POST -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" $SERVICE_URL"
