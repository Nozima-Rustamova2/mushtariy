# Mushtariy Instagram MCP server

An MCP server that lets Claude publish to **@mushtariy.beta** through Meta's official Instagram Graph API (Facebook Login flow, app "DressCode Publisher"). It runs on Cloud Run in the Google Cloud project `dresscode-social`.

**Tools:** `publish_photo`, `publish_carousel`, `publish_reel`, `publish_story`, `stage_media`, `schedule_post`, `list_scheduled`, `cancel_scheduled`, `list_recent`, `get_insights`, `account_insights`, `list_comments`, `reply_comment`, `hide_comment`, `delete_post`, `whoami`.

**Rules built in:**
- Every post is sent with the AI-generated flag (`is_ai_generated=true`).
- Media is copied into the public bucket `dresscode-social-media` before publishing. Copies are auto-deleted after 30 days.
- Scheduled posts sit in `gs://dresscode-social-media/queue/`. Cloud Scheduler calls `/run-queue` every 5 minutes to publish whatever is due.

## Setup (once)

1. **Meta token.** In Graph API Explorer:
   - Choose the app *DressCode Publisher* and get a *User Token* with `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`, `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement` and `business_management`.
   - Exchange it for a long-lived token. Then call `GET /me/accounts`: the Page token returned for **Mushtariy** never expires.
   - Call `GET /<page-id>?fields=instagram_business_account`. The id it returns is your `IG_USER_ID`.
2. **Secret Manager** (project `dresscode-social`):
   - Create the secret `META_ACCESS_TOKEN` and paste the Page token into it.
   - Create the secret `IG_USER_ID` and paste the id into it.
   - Never paste either one into a chat.
3. **Cloud Shell.** Run `git clone https://github.com/Nozima-Rustamova2/mushtariy && cd mushtariy && bash deploy.sh`.
4. **Connect it.** Add the printed URL in claude.ai under Settings → Connectors → Add custom connector.

The secret path in the URL is the only key. If it leaks, add a new version of `MCP_PATH_TOKEN` and redeploy.
