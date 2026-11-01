# Mushtariy Instagram MCP server

An MCP server that lets Claude publish to **@mushtariy.beta** through Meta's official Instagram Graph API (Facebook Login flow, app "DressCode Publisher"). It runs on Cloud Run in the Google Cloud project `dresscode-social`.

**Tools:** `publish_photo`, `publish_carousel`, `publish_reel`, `publish_story`, `stage_media`, `schedule_post`, `list_scheduled`, `cancel_scheduled`, `list_recent`, `get_insights`, `account_insights`, `list_comments`, `reply_comment`, `hide_comment`, `delete_post`, `whoami`.

**Rules built in:**
- Every post is sent with the AI-generated flag (`is_ai_generated=true`).
- Everything lives in one **private** bucket, `dresscode-social-media`:
  - `media/`: copies of every image and video. The service hands them to Meta at `<service-url>/m/<random-name>`; nothing in the bucket is public.
  - `queue/`: scheduled posts. Cloud Scheduler calls `/run-queue` every 5 minutes to publish whatever is due.
  - `config/mcp_path_token`: the **access code** you type once when connecting Claude.
  - `oauth/clients/`: the apps that have registered for sign-in (claude.ai).
- Runs as the service account `mushtariy-mcp`. Secrets in Secret Manager: `META_ACCESS_TOKEN` and `IG_USER_ID`.
- **Connector URL:** `<service-url>/mcp`. Claude registers itself and opens `/login`, where you type the access code once. Access tokens last 7 days and refresh automatically. Changing the access code (delete `config/mcp_path_token` and redeploy) signs everyone out.

## Setup (once)

1. **Meta token.** In Graph API Explorer:
   - Choose the app *DressCode Publisher* and get a *User Token* with `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`, `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement` and `business_management`.
   - Exchange it for a long-lived token. Then call `GET /me/accounts`: the Page token returned for **Mushtariy** never expires.
   - Call `GET /<page-id>?fields=instagram_business_account`. The id it returns is your `IG_USER_ID`.
2. **Secret Manager** (project `dresscode-social`):
   - Create the secret `META_ACCESS_TOKEN` and paste the Page token into it.
   - Create the secret `IG_USER_ID` and paste the id into it.
   - Never paste either one into a chat.
3. **Deploy.** Either run `bash deploy.sh` in Cloud Shell, or deploy through the console (Cloud Run → connect this repo, Dockerfile build) with env `BUCKET=dresscode-social-media` and the two secrets as env vars.
4. **Connect it.** In claude.ai, go to Settings → Connectors → Add custom connector and use `<service-url>/mcp`. Click Connect and enter the access code.

The access code is the only key. If it leaks, rotate it as described above.
