#!/usr/bin/env bash
# One-time setup + deploy for the Mushtariy Instagram MCP server.
# Run in Google Cloud Shell (it's already logged in):  bash deploy.sh
set -euo pipefail
PROJECT=dresscode-social
REGION=europe-west1
BUCKET=${PROJECT}-media
SA=mushtariy-mcp@${PROJECT}.iam.gserviceaccount.com

gcloud config set project "$PROJECT"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com

# Public media bucket (Meta must be able to fetch media by URL)
gcloud storage buckets describe gs://$BUCKET >/dev/null 2>&1 || \
  gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=allUsers --role=roles/storage.objectViewer
# auto-delete staged media after 30 days (queue/ files are removed when published)
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30,"matchesPrefix":["media/"]}}]}' > /tmp/lc.json
gcloud storage buckets update gs://$BUCKET --lifecycle-file=/tmp/lc.json

# Service account the server runs as
gcloud iam service-accounts describe $SA >/dev/null 2>&1 || \
  gcloud iam service-accounts create mushtariy-mcp --display-name="Mushtariy MCP"
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=serviceAccount:$SA --role=roles/storage.objectAdmin

# Generated secrets (random, never shown in chat)
for S in MCP_PATH_TOKEN QUEUE_TOKEN; do
  gcloud secrets describe $S >/dev/null 2>&1 || \
    python3 -c "import secrets;print(secrets.token_urlsafe(32),end='')" | gcloud secrets create $S --data-file=-
done
# META_ACCESS_TOKEN and IG_USER_ID are added by you in Secret Manager (see README)
for S in MCP_PATH_TOKEN QUEUE_TOKEN META_ACCESS_TOKEN IG_USER_ID; do
  gcloud secrets add-iam-policy-binding $S --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor >/dev/null
done

gcloud run deploy mushtariy-mcp --source . --region=$REGION --service-account=$SA \
  --allow-unauthenticated --min-instances=0 --max-instances=1 --memory=512Mi --timeout=900 \
  --set-env-vars=MEDIA_BUCKET=$BUCKET \
  --set-secrets=MCP_PATH_TOKEN=MCP_PATH_TOKEN:latest,QUEUE_TOKEN=QUEUE_TOKEN:latest,META_ACCESS_TOKEN=META_ACCESS_TOKEN:latest,IG_USER_ID=IG_USER_ID:latest

URL=$(gcloud run services describe mushtariy-mcp --region=$REGION --format='value(status.url)')

# Publish due scheduled posts every 5 minutes
QT=$(gcloud secrets versions access latest --secret=QUEUE_TOKEN)
gcloud scheduler jobs describe mushtariy-queue --location=$REGION >/dev/null 2>&1 && \
  gcloud scheduler jobs delete mushtariy-queue --location=$REGION --quiet
gcloud scheduler jobs create http mushtariy-queue --location=$REGION --schedule="*/5 * * * *" \
  --uri="$URL/run-queue" --http-method=POST --headers="x-queue-token=$QT"

echo
echo "Connector URL (keep private — it is the key):"
echo "$URL/mcp/$(gcloud secrets versions access latest --secret=MCP_PATH_TOKEN)"
