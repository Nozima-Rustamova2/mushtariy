"""Mushtariy Instagram publisher — an MCP server on Meta's official Instagram Graph API.

Runs on Cloud Run. Claude connects to it as a custom connector and can then publish
photos, carousels, Reels and Stories to @mushtariy.beta, schedule posts, read
insights and handle comments.

Every post is sent with the AI-generated label on. That is a fixed rule here, not an option.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

# ---------------------------------------------------------------- config

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v24.0")
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")  # long-lived Page token (Secret Manager)
IG_USER_ID = os.environ.get("IG_USER_ID", "")  # Instagram professional account id
# One PRIVATE bucket: media/ (served to Meta through this service), queue/, config/.
BUCKET = os.environ.get("BUCKET", "")
STATE_BUCKET = BUCKET
# Public base URL of this service, used to build media links Meta can fetch.
# Learned from the first incoming request if not set.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
MCP_PATH_TOKEN = os.environ.get("MCP_PATH_TOKEN", "")  # secret path segment for the connector URL
# Field Meta uses for the AI-generated disclosure. Kept configurable in case Meta renames it.
AI_LABEL_PARAM = os.environ.get("AI_LABEL_PARAM", "is_ai_generated")



def _state_bucket():
    from google.cloud import storage

    return storage.Client().bucket(STATE_BUCKET)


def _bootstrap_path_token() -> str:
    """The connector URL's secret segment. Generated once and kept in the private bucket
    (config/mcp_path_token), so nobody has to type a secret anywhere. Rotate it by
    deleting that object and restarting the service."""
    if not STATE_BUCKET:
        print("STATE_BUCKET not set; using a throwaway path token for this process.")
        return secrets.token_urlsafe(24)
    from google.api_core.exceptions import PreconditionFailed

    blob = _state_bucket().blob("config/mcp_path_token")
    try:
        blob.upload_from_string(secrets.token_urlsafe(32), if_generation_match=0)
    except PreconditionFailed:
        pass  # already exists
    return blob.download_as_text().strip()


if not MCP_PATH_TOKEN:
    MCP_PATH_TOKEN = _bootstrap_path_token()

mcp = FastMCP(
    name="mushtariy-instagram",
    instructions=(
        "Publishes to the Instagram account @mushtariy.beta (an openly AI virtual model). "
        "Media must be a public URL; non-bucket URLs are copied to the project's bucket first. "
        "Every post is labelled AI-generated automatically. Show the user the caption and "
        "media and get a clear yes before calling any publish_* or delete_post tool."
    ),
    host="0.0.0.0",
    stateless_http=True,
    json_response=True,
    streamable_http_path=f"/mcp/{MCP_PATH_TOKEN}",
)


class GraphError(RuntimeError):
    pass


# ---------------------------------------------------------------- Graph API helpers


async def graph(method: str, path: str, **params: Any) -> dict:
    if not ACCESS_TOKEN or not IG_USER_ID:
        raise GraphError("Server is missing META_ACCESS_TOKEN or IG_USER_ID.")
    params = {k: v for k, v in params.items() if v is not None}
    params["access_token"] = ACCESS_TOKEN
    async with httpx.AsyncClient(timeout=60) as client:
        if method == "GET":
            r = await client.get(f"{GRAPH}/{path}", params=params)
        else:
            r = await client.request(method, f"{GRAPH}/{path}", data=params)
    body = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in body:
        err = body.get("error", {})
        raise GraphError(
            f"Graph API {r.status_code}: {err.get('message', r.text)} "
            f"(code {err.get('code')}, subcode {err.get('error_subcode')})"
        )
    return body


async def wait_until_ready(container_id: str, timeout_s: int = 300) -> None:
    """Video containers process asynchronously; poll until FINISHED."""
    deadline = time.monotonic() + timeout_s
    while True:
        info = await graph("GET", container_id, fields="status_code,status")
        code = info.get("status_code")
        if code in ("FINISHED", "PUBLISHED"):
            return
        if code in ("ERROR", "EXPIRED"):
            raise GraphError(f"Media processing failed: {info.get('status')}")
        if time.monotonic() > deadline:
            raise GraphError(f"Media still processing after {timeout_s}s (container {container_id}).")
        await asyncio.sleep(5)


async def publish_container(container_id: str) -> dict:
    res = await graph("POST", f"{IG_USER_ID}/media_publish", creation_id=container_id)
    media_id = res["id"]
    info = await graph("GET", media_id, fields="id,permalink,media_type,timestamp")
    return info


def labelled(params: dict) -> dict:
    params[AI_LABEL_PARAM] = "true"
    return params


# ---------------------------------------------------------------- media staging
# Media stays in the private bucket. This service serves it at /m/<random-name>, which is
# the URL handed to Meta. Names are 128-bit random, so links can't be guessed.


def _public_url(blob_name: str) -> str:
    if not PUBLIC_BASE_URL:
        raise GraphError("Service URL not known yet; set PUBLIC_BASE_URL.")
    return f"{PUBLIC_BASE_URL}/m/{blob_name.removeprefix('media/')}"


def _is_our_bucket(url: str) -> bool:
    return bool(PUBLIC_BASE_URL) and url.startswith(f"{PUBLIC_BASE_URL}/m/")


@mcp.custom_route("/m/{name:path}", methods=["GET", "HEAD"])
async def serve_media(request: Request):
    from starlette.responses import Response

    name = request.path_params["name"]
    if ".." in name or not name:
        return Response(status_code=404)
    blob = _state_bucket().blob(f"media/{name}")
    try:
        data = await asyncio.to_thread(blob.download_as_bytes)
    except Exception:
        return Response(status_code=404)
    ctype = blob.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    body = b"" if request.method == "HEAD" else data
    return Response(body, media_type=ctype, headers={"content-length": str(len(data)), "cache-control": "public, max-age=86400"})


async def stage(url: str) -> str:
    """Copy a media URL into the public bucket so Meta can always fetch it."""
    if not BUCKET or _is_our_bucket(url):
        return url
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
    ctype = r.headers.get("content-type", "").split(";")[0] or "application/octet-stream"
    ext = mimetypes.guess_extension(ctype) or os.path.splitext(urlparse(url).path)[1] or ""
    if ext == ".jpe":
        ext = ".jpg"
    name = f"media/{datetime.now(timezone.utc):%Y%m%d}/{uuid.uuid4().hex}{ext}"

    def _upload() -> None:
        from google.cloud import storage

        storage.Client().bucket(BUCKET).blob(name).upload_from_string(r.content, content_type=ctype)

    await asyncio.to_thread(_upload)
    return _public_url(name)


# ---------------------------------------------------------------- publishing tools


@mcp.tool()
async def stage_media(url: str) -> dict:
    """Copy an image or video (e.g. a Higgsfield result URL) into the public bucket and return its stable URL."""
    return {"url": await stage(url)}


@mcp.tool()
async def publish_photo(image_url: str, caption: str = "", location_id: str | None = None) -> dict:
    """Publish one JPEG photo to the feed. Labelled AI-generated."""
    c = await graph(
        "POST",
        f"{IG_USER_ID}/media",
        **labelled({"image_url": await stage(image_url), "caption": caption, "location_id": location_id}),
    )
    return await publish_container(c["id"])


@mcp.tool()
async def publish_carousel(media_urls: list[str], caption: str = "") -> dict:
    """Publish a carousel of 2–10 images and/or videos. Video URLs must end in .mp4 or .mov."""
    if not 2 <= len(media_urls) <= 10:
        raise GraphError("A carousel needs 2 to 10 items.")
    children = []
    for url in media_urls:
        staged = await stage(url)
        is_video = urlparse(staged).path.lower().endswith((".mp4", ".mov"))
        params = {"is_carousel_item": "true"}
        if is_video:
            params.update(media_type="VIDEO", video_url=staged)
        else:
            params["image_url"] = staged
        child = await graph("POST", f"{IG_USER_ID}/media", **labelled(params))
        if is_video:
            await wait_until_ready(child["id"])
        children.append(child["id"])
    c = await graph(
        "POST",
        f"{IG_USER_ID}/media",
        **labelled({"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption}),
    )
    await wait_until_ready(c["id"])
    return await publish_container(c["id"])


@mcp.tool()
async def publish_reel(
    video_url: str,
    caption: str = "",
    cover_url: str | None = None,
    thumb_offset_ms: int | None = None,
    share_to_feed: bool = True,
    audio_name: str | None = None,
) -> dict:
    """Publish a Reel (MP4, 9:16, 3s–15min). audio_name renames the original audio track."""
    c = await graph(
        "POST",
        f"{IG_USER_ID}/media",
        **labelled(
            {
                "media_type": "REELS",
                "video_url": await stage(video_url),
                "caption": caption,
                "cover_url": await stage(cover_url) if cover_url else None,
                "thumb_offset": thumb_offset_ms,
                "share_to_feed": "true" if share_to_feed else "false",
                "audio_name": audio_name,
            }
        ),
    )
    await wait_until_ready(c["id"], timeout_s=600)
    return await publish_container(c["id"])


@mcp.tool()
async def publish_story(media_url: str) -> dict:
    """Publish a Story from one image or one video (.mp4/.mov)."""
    staged = await stage(media_url)
    is_video = urlparse(staged).path.lower().endswith((".mp4", ".mov"))
    params = {"media_type": "STORIES"}
    params["video_url" if is_video else "image_url"] = staged
    c = await graph("POST", f"{IG_USER_ID}/media", **labelled(params))
    if is_video:
        await wait_until_ready(c["id"])
    return await publish_container(c["id"])


# ---------------------------------------------------------------- scheduling (queue lives in the bucket)

POST_KINDS = {"photo", "carousel", "reel", "story"}


def _queue_blobs():
    return _state_bucket().list_blobs(prefix="queue/")


@mcp.tool()
async def schedule_post(kind: str, publish_at_iso: str, args: dict) -> dict:
    """Queue a post for later. kind: photo|carousel|reel|story. publish_at_iso: ISO 8601 with
    timezone, e.g. 2026-11-03T19:00:00+05:00 (Tashkent). args: the same arguments the matching
    publish_* tool takes. Media is copied to the bucket now so the links can't expire."""
    if kind not in POST_KINDS:
        raise GraphError(f"kind must be one of {sorted(POST_KINDS)}")
    when = datetime.fromisoformat(publish_at_iso)
    if when.tzinfo is None:
        raise GraphError("publish_at_iso needs a timezone offset, e.g. +05:00.")
    for key in ("image_url", "video_url", "media_url", "cover_url"):
        if args.get(key):
            args[key] = await stage(args[key])
    if args.get("media_urls"):
        args["media_urls"] = [await stage(u) for u in args["media_urls"]]
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "at": when.astimezone(timezone.utc).isoformat(), "args": args}

    def _save() -> None:
        _state_bucket().blob(f"queue/{job['id']}.json").upload_from_string(
            json.dumps(job), content_type="application/json"
        )

    await asyncio.to_thread(_save)
    return {"scheduled": job}


@mcp.tool()
async def list_scheduled() -> list[dict]:
    """List queued posts that haven't been published yet."""
    blobs = await asyncio.to_thread(lambda: list(_queue_blobs()))
    jobs = [json.loads(b.download_as_text()) for b in blobs]
    return sorted(jobs, key=lambda j: j["at"])


@mcp.tool()
async def cancel_scheduled(job_id: str) -> dict:
    """Remove a queued post by its id."""
    def _del() -> None:
        _state_bucket().blob(f"queue/{job_id}.json").delete()

    await asyncio.to_thread(_del)
    return {"cancelled": job_id}


async def _run(kind: str, args: dict) -> dict:
    fn = {"photo": publish_photo, "carousel": publish_carousel, "reel": publish_reel, "story": publish_story}[kind]
    return await fn(**args)


@mcp.custom_route("/run-queue", methods=["POST"])
async def run_queue(_: Request) -> JSONResponse:
    """Called by Cloud Scheduler every 5 minutes; publishes whatever is due.

    Needs no secret: it can only publish posts that were already queued through the
    (secret-URL) MCP endpoint, and only once they are due. Each job is claimed with a
    create-only lock object first, so overlapping calls can't publish a post twice."""
    from google.api_core.exceptions import PreconditionFailed

    now = datetime.now(timezone.utc)
    results = []
    for blob in await asyncio.to_thread(lambda: list(_queue_blobs())):
        job = json.loads(await asyncio.to_thread(blob.download_as_text))
        if datetime.fromisoformat(job["at"]) > now:
            continue
        lock = _state_bucket().blob(f"running/{job['id']}")
        try:
            await asyncio.to_thread(lock.upload_from_string, now.isoformat(), if_generation_match=0)
        except PreconditionFailed:
            continue  # another run has it
        try:
            out = await _run(job["kind"], job["args"])
            await asyncio.to_thread(blob.delete)
            results.append({"id": job["id"], "ok": True, "permalink": out.get("permalink")})
        except Exception as e:  # keep the job for the next run, record the error
            job["last_error"] = str(e)
            job["attempts"] = job.get("attempts", 0) + 1
            if job["attempts"] >= 3:
                await asyncio.to_thread(
                    _state_bucket().blob(f"failed/{job['id']}.json").upload_from_string, json.dumps(job)
                )
                await asyncio.to_thread(blob.delete)
            else:
                await asyncio.to_thread(blob.upload_from_string, json.dumps(job), content_type="application/json")
            results.append({"id": job["id"], "ok": False, "error": str(e)})
        finally:
            await asyncio.to_thread(lock.delete)
    return JSONResponse({"ran": results})


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- reading & community


@mcp.tool()
async def list_recent(limit: int = 12) -> list[dict]:
    """Recent posts with caption, type, permalink, likes and comment counts."""
    res = await graph(
        "GET",
        f"{IG_USER_ID}/media",
        fields="id,caption,media_type,permalink,timestamp,like_count,comments_count",
        limit=min(limit, 50),
    )
    return res.get("data", [])


@mcp.tool()
async def get_insights(media_id: str) -> dict:
    """Per-post insights: reach, views, likes, comments, shares, saves, total interactions."""
    res = await graph(
        "GET",
        f"{media_id}/insights",
        metric="reach,views,likes,comments,shares,saved,total_interactions",
    )
    return {m["name"]: (m.get("values") or [{}])[0].get("value", m.get("total_value", {}).get("value")) for m in res.get("data", [])}


@mcp.tool()
async def account_insights(days: int = 7) -> dict:
    """Account totals over the last N days (max 30), plus current follower count and publishing quota."""
    until = int(time.time())
    since = until - min(days, 30) * 86400
    res = await graph(
        "GET",
        f"{IG_USER_ID}/insights",
        metric="reach,views,accounts_engaged,total_interactions,follows_and_unfollows",
        period="day",
        metric_type="total_value",
        since=since,
        until=until,
    )
    profile = await graph("GET", IG_USER_ID, fields="username,followers_count,media_count")
    quota = await graph("GET", f"{IG_USER_ID}/content_publishing_limit", fields="quota_usage,config")
    totals = {m["name"]: m.get("total_value", {}).get("value") for m in res.get("data", [])}
    return {"profile": profile, "last_days": days, "totals": totals, "publishing_quota": quota.get("data")}


@mcp.tool()
async def list_comments(media_id: str) -> list[dict]:
    """Comments (with replies) on a post."""
    res = await graph(
        "GET",
        f"{media_id}/comments",
        fields="id,text,username,timestamp,like_count,replies{id,text,username,timestamp}",
    )
    return res.get("data", [])


@mcp.tool()
async def reply_comment(comment_id: str, message: str) -> dict:
    """Reply to a comment as Mushtariy. Confirm the wording with the user first."""
    return await graph("POST", f"{comment_id}/replies", message=message)


@mcp.tool()
async def hide_comment(comment_id: str, hide: bool = True) -> dict:
    """Hide (or unhide) a comment."""
    return await graph("POST", comment_id, hide="true" if hide else "false")


@mcp.tool()
async def delete_post(media_id: str) -> dict:
    """Delete a published post. Irreversible — only after the user explicitly says so."""
    return await graph("DELETE", media_id)


@mcp.tool()
async def whoami() -> dict:
    """Check the connection: which Instagram account and Page this server publishes to."""
    return await graph("GET", IG_USER_ID, fields="id,username,name,followers_count,media_count")


app = mcp.streamable_http_app()


class _LearnBaseURL:
    """Remember the service's public https URL from the first request it receives."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        global PUBLIC_BASE_URL
        if scope["type"] == "http" and not PUBLIC_BASE_URL:
            host = dict(scope["headers"]).get(b"host", b"").decode()
            if host and not host.startswith(("localhost", "127.")):
                PUBLIC_BASE_URL = f"https://{host}"
        await self.inner(scope, receive, send)


app = _LearnBaseURL(app)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
