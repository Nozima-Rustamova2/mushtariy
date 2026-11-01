#!/usr/bin/env bash
# One-time setup + deploy for the Mushtariy Instagram MCP server.
# Run in Google Cloud Shell (it's already logged in):  bash deploy.sh
set -euo pipefail
PROJECT=dresscode-social
REGION=us-east1
BUCKET=${PROJECT}-media
SA=mushtariy-mcp@${PROJECT}.iam.gserviceaccount.com

gcloud config set project "$PROJECT"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com

# Private bucket (media/, queue/, config/) — media is served to Meta by the service itself
gcloud storage buckets describe gs://$BUCKET >/dev/null 2>&1 || \
  gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access --public-access-prevention
# auto-delete staged media after 30 days (queue/ files are removed when published)
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30,"matchesPrefix":["media/"]}}]}' > /tmp/lc.json
gcloud storage buckets update gs://$BUCKET --lifecycle-file=/tmp/lc.json

# Service account the server runs as
gcloud iam service-accounts describe $SA >/dev/null 2>&1 || \
  gcloud iam service-accounts create mushtariy-mcp --display-name="Mushtariy MCP"
for B in $BUCKET; do
  gcloud storage buckets add-iam-policy-binding gs://$B --member=serviceAccount:$SA --role=roles/storage.objectAdmin
done

# META_ACCESS_TOKEN and IG_USER_ID are added by you in Secret Manager (see README)
for S in META_ACCESS_TOKEN IG_USER_ID; do
  gcloud secrets add-iam-policy-binding $S --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor >/dev/null
done

gcloud run deploy mushtariy-mcp --source . --region=$REGION --service-account=$SA \
  --allow-unauthenticated --min-instances=0 --max-instances=1 --memory=512Mi --timeout=900 \
  --set-env-vars=BUCKET=$BUCKET \
  --set-secrets=META_ACCESS_TOKEN=META_ACCESS_TOKEN:latest,IG_USER_ID=IG_USER_ID:latest

URL=$(gcloud run services describe mushtariy-mcp --region=$REGION --format='value(status.url)')

# Publish due scheduled posts every 5 minutes
gcloud scheduler jobs describe mushtariy-queue --location=$REGION >/dev/null 2>&1 && \
  gcloud scheduler jobs delete mushtariy-queue --location=$REGION --quiet
gcloud scheduler jobs create http mushtariy-queue --location=$REGION --schedule="*/5 * * * *" \
  --uri="$URL/run-queue" --http-method=POST

echo
echo "Connector URL (keep private — it is the key):"
curl -s "$URL/health" >/dev/null   # first request creates the path token
echo "$URL/mcp/$(gcloud storage cat gs://$BUCKET/config/mcp_path_token)"
