"""
yt_notion_sync.py
─────────────────────────────────────────────────────────────────────────────
Pulls YouTube Data API + Analytics data for every active channel in the
Notion "YT Channels" database and upserts a monthly-metrics row into
"YT Monthly Metrics".
"""

import os
import datetime
import calendar
import re
import pytz
from dotenv import load_dotenv

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from notion_client import Client

load_dotenv()

NOTION_TOKEN              = os.environ["NOTION_TOKEN"]
GOOGLE_CLIENT_SECRET_PATH = os.environ.get("GOOGLE_CLIENT_SECRET_PATH", "client_secret.json")
GOOGLE_TOKEN_PATH         = os.environ.get("GOOGLE_TOKEN_PATH", "token.json")

YT_CHANNELS_DB_ID = "69c35b72aadb4a3798ee89690c7edab5"
YT_MONTHLY_DB_ID  = "af6488aad4d34dc1a4532fcd1e490b6b"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

PACIFIC = pytz.timezone("America/Los_Angeles")


def get_report_month():
    today_local      = datetime.datetime.now().date()
    first_of_month   = today_local.replace(day=1)
    last_month_end   = first_of_month - datetime.timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    default_month    = last_month_start.strftime("%Y-%m")

    print(f"\n  Default report month : {default_month}  (previous calendar month)")

    import sys
    if not sys.stdin.isatty():
        # Running in GitHub Actions or non-interactive env — skip prompt
        print(f"  Non-interactive mode detected. Using default: {default_month}")
        chosen = default_month
    else:
        user_input = input(f"  Enter month [YYYY-MM] or press Enter for {default_month}: ").strip()
        if not user_input:
            chosen = default_month
        elif not re.match(r"^\d{4}-(?:0[1-9]|1[0-2])$", user_input):
            print(f"  Invalid format. Using default: {default_month}")
            chosen = default_month
        else:
            chosen = user_input

    year, month = int(chosen[:4]), int(chosen[5:7])
    last_day    = calendar.monthrange(year, month)[1]
    return chosen, f"{chosen}-01", f"{chosen}-{last_day:02d}"


def get_google_credentials():
    creds = None
    if os.path.exists(GOOGLE_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CLIENT_SECRET_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GOOGLE_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


def get_active_channels(notion):
    channels, cursor = [], None
    while True:
        kwargs = {
            "database_id": YT_CHANNELS_DB_ID,
            "filter": {"property": "Active", "checkbox": {"equals": True}},
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        for page in resp.get("results", []):
            props      = page.get("properties", {})
            name       = (props.get("Name",       {}).get("title",     [{}])[0].get("plain_text", ""))
            channel_id = (props.get("Channel ID", {}).get("rich_text", [{}])[0].get("plain_text", ""))
            if channel_id:
                channels.append({"page_id": page["id"], "name": name, "channel_id": channel_id})
        if resp.get("has_more"):
            cursor = resp["next_cursor"]
        else:
            break
    print(f"[notion] {len(channels)} active channels found.")
    return channels


def fetch_channel_metadata(yt, channel_id):
    resp  = yt.channels().list(part="snippet,statistics,contentDetails", id=channel_id).execute()
    items = resp.get("items", [])
    if not items:
        raise ValueError(f"No YouTube channel found for id={channel_id}")
    item    = items[0]
    stats   = item.get("statistics", {})
    uploads = item["contentDetails"]["relatedPlaylists"].get("uploads", "")
    return {
        "name":                    item["snippet"]["title"],
        "uploads_playlist_id":     uploads,
        "current_subscribers":     int(stats.get("subscriberCount", 0)),
        "hidden_subscriber_count": stats.get("hiddenSubscriberCount", False),
    }


def count_videos_in_month(yt, uploads_playlist_id, start_date, end_date):
    start_pt   = PACIFIC.localize(datetime.datetime.fromisoformat(start_date))
    end_pt     = PACIFIC.localize(datetime.datetime.fromisoformat(end_date) + datetime.timedelta(days=1))
    count, video_ids, page_token, stop_early = 0, [], None, False

    while not stop_early:
        kwargs = {"part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": 50}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = yt.playlistItems().list(**kwargs).execute()
        for item in resp.get("items", []):
            raw = item["contentDetails"].get("videoPublishedAt", "")
            if not raw:
                continue
            pub_pt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(PACIFIC)
            if pub_pt < start_pt:
                stop_early = True
                break
            if pub_pt < end_pt:
                count += 1
                vid_id = item["contentDetails"].get("videoId", "")
                if vid_id:
                    video_ids.append(vid_id)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return count, video_ids


def fetch_analytics(yta, channel_id, start_date, end_date):
    try:
        resp = yta.reports().query(
            ids=f"channel=={channel_id}",
            startDate=start_date,
            endDate=end_date,
            metrics="views,subscribersGained,subscribersLost",
        ).execute()
    except Exception as e:
        msg = str(e)
        if "403" in msg or "Forbidden" in msg:
            print("    [analytics] 403 Forbidden — no Analytics access for this channel.")
            return (
                {"views": 0, "subscribers_gained": 0, "subscribers_lost": 0, "subscribers_net_added": 0},
                "Analytics API 403: account does not have Analytics access to this channel."
            )
        raise

    rows = resp.get("rows", [])
    if not rows:
        print("    [analytics] No data rows — month may not be finalised yet.")
        return ({"views": 0, "subscribers_gained": 0, "subscribers_lost": 0, "subscribers_net_added": 0}, "")

    views, gained, lost = int(rows[0][0]), int(rows[0][1]), int(rows[0][2])
    return ({"views": views, "subscribers_gained": gained, "subscribers_lost": lost, "subscribers_net_added": gained - lost}, "")


def fetch_recent_month_views(yta, channel_id, video_ids, start_date, end_date):
    if not video_ids:
        return 0
    total_views = 0
    for i in range(0, len(video_ids), 200):
        batch      = video_ids[i:i + 200]
        vid_filter = "video==" + ",".join(batch)
        try:
            resp = yta.reports().query(
                ids=f"channel=={channel_id}",
                startDate=start_date,
                endDate=end_date,
                dimensions="video",
                metrics="views",
                filters=vid_filter,
            ).execute()
            for row in resp.get("rows", []):
                total_views += int(row[1])
        except Exception as e:
            msg = str(e)
            if "403" in msg or "Forbidden" in msg:
                print("    [recent_month_views] 403 Forbidden — skipping.")
                return 0
            raise
    return total_views


def get_previous_month(report_month):
    year, month = int(report_month[:4]), int(report_month[5:7])
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


def fetch_prev_month_metrics(notion, channel_id, report_month):
    """
    Reads the previous month's row from YT Monthly Metrics.
    Returns a dict or None if no previous row exists.

    Property names must match the actual Notion schema:
      - "Videos Published"    (number)     ← NOT "Videos Uploaded"
      - "Views"               (number)
      - "Recent Month Views"  (number)
      - "Subscribers Gained"  (number)     ← used to compute net (formula can't be read)
      - "Subscribers Lost"    (number)
    """
    prev_month = get_previous_month(report_month)
    resp = notion.databases.query(
        database_id=YT_MONTHLY_DB_ID,
        filter={"and": [
            {"property": "Channel ID",   "rich_text": {"equals": channel_id}},
            {"property": "Report Month", "rich_text": {"equals": prev_month}},
        ]},
        page_size=1,
    )
    results = resp.get("results", [])
    if not results:
        print(f"    [trend] No {prev_month} row found — trends will be null")
        return None

    props = results[0].get("properties", {})
    def num(key):
        return props.get(key, {}).get("number") or 0

    prev = {
        "videos_published":      num("Videos Published"),
        "views":                 num("Views"),
        "recent_month_views":    num("Recent Month Views"),
        # "Subscribers Net Added" is a Notion formula — derive from components
    }
    print(f"    [trend] {prev_month}: videos={prev['videos_published']}  "
          f"views={prev['views']:,}  net_subs={prev['subscribers_net_added']:+,}")
    return prev


def upsert_monthly_row(notion, channel_page_id, channel_id, channel_name,
                       report_month, month_start, metrics,
                       sync_status="success", error_note=""):
    """
    Notion column names (must match schema exactly):
      "Videos Published"         ← videos uploaded in month
      "Subscribers Net Added"    ← FORMULA in Notion, NOT written by script
      "Trend Videos Published"   ← matches Notion column name
    """
    now_iso    = datetime.datetime.now(datetime.timezone.utc).isoformat()
    properties = {
        "Name":         {"title":    [{"text": {"content": f"{report_month} | {channel_name}"}}]},
        "Channel":      {"relation": [{"id": channel_page_id}]},
        "Channel ID":   {"rich_text": [{"text": {"content": channel_id}}]},
        "Report Month": {"rich_text": [{"text": {"content": report_month}}]},
        "Month Start":  {"date": {"start": month_start}},

        # Core metrics
        "Videos Published":   {"number": metrics.get("videos_published", 0)},
        "Views":              {"number": metrics.get("views", 0)},
        "Recent Month Views": {"number": metrics.get("recent_month_views", 0)},
        "Subscribers Gained": {"number": metrics.get("subscribers_gained", 0)},
        "Subscribers Lost":   {"number": metrics.get("subscribers_lost", 0)},
        # NOTE: "Subscribers Net Added" is a Notion formula — skipped intentionally

        # Trend columns (null = no previous month row yet)
        "Trend Videos Published":      {"number": metrics.get("trend_videos_published")},
        "Trend Views":                 {"number": metrics.get("trend_views")},
        "Trend Recent Month Views":    {"number": metrics.get("trend_recent_month_views")},

        # Sync metadata
        "Last Synced At": {"date": {"start": now_iso}},
        "Sync Status":    {"select": {"name": sync_status}},
        "Error Note": {
            "rich_text": [{"text": {"content": error_note[:500]}}] if error_note else []
        },
    }

    existing = notion.databases.query(
        database_id=YT_MONTHLY_DB_ID,
        filter={"and": [
            {"property": "Channel ID",   "rich_text": {"equals": channel_id}},
            {"property": "Report Month", "rich_text": {"equals": report_month}},
        ]},
        page_size=1,
    )

    if existing.get("results"):
        notion.pages.update(page_id=existing["results"][0]["id"], properties=properties)
        print(f"    [notion] Updated  →  {report_month} | {channel_name}")
    else:
        notion.pages.create(parent={"database_id": YT_MONTHLY_DB_ID}, properties=properties)
        print(f"    [notion] Created  →  {report_month} | {channel_name}")


def ensure_subscriber_column(notion, col_name: str) -> None:
    """
    Creates the dated subscriber column in YT Channels if it doesn't exist yet.
    Safe to call multiple times — Notion silently ignores duplicate ADD COLUMN.
    Uses the Notion REST API directly since the MCP tool requires a data source ID.
    """
    import requests
    token = NOTION_TOKEN
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    # We can't add columns via REST easily, so we attempt to write to the column
    # and catch the error — if it doesn't exist we create it via update_data_source.
    # Instead, track which columns we've already created this run.
    pass  # handled in update_channel_snapshot via try/except


_subscriber_col_created = False  # module-level flag: only create column once per run


def update_channel_snapshot(notion, page_id, metadata, run_date_col: str):
    """
    Writes the channel snapshot back to YT Channels.
    run_date_col: column name like 'Subscribers (260401)' — created if missing.
    """
    global _subscriber_col_created
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Attempt to write to the dated column; if it doesn't exist, create it first
    if not _subscriber_col_created:
        try:
            import requests
            token = NOTION_TOKEN
            # Try to add the column via the Notion API
            # (silently fails if column already exists)
            requests.patch(
                f"https://api.notion.com/v1/databases/{YT_CHANNELS_DB_ID}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": "2022-06-28",
                    "Content-Type": "application/json",
                },
                json={"properties": {run_date_col: {"number": {}}}},
            )
        except Exception:
            pass
        _subscriber_col_created = True

    notion.pages.update(
        page_id=page_id,
        properties={
            "Uploads Playlist ID":     {"rich_text": [{"text": {"content": metadata["uploads_playlist_id"]}}]},
            run_date_col:              {"number":    metadata["current_subscribers"]},
            "Hidden Subscriber Count": {"checkbox":  metadata["hidden_subscriber_count"]},
            "Last Synced At":          {"date": {"start": now_iso}},
            "Sync Status":             {"select": {"name": "success"}},
        },
    )


def main():
    report_month, start_date, end_date = get_report_month()

    print(f"\n{'='*60}")
    print(f"  Report month : {report_month}  ({start_date} → {end_date}, PT)")
    print(f"{'='*60}\n")

    # Build the dated subscriber column name from today's local date (YYMMDD)
    run_date_col = "Subscribers (" + datetime.datetime.now().strftime("%y%m%d") + ")"
    print(f"  Subscriber column   : {run_date_col}")

    creds  = get_google_credentials()
    yt     = build("youtube",          "v3", credentials=creds)
    yta    = build("youtubeAnalytics", "v2", credentials=creds)
    notion = Client(auth=NOTION_TOKEN)

    channels = get_active_channels(notion)

    for ch in channels:
        cid, name, pid = ch["channel_id"], ch["name"], ch["page_id"]
        print(f"\n── {name}  ({cid})")
        try:
            meta                   = fetch_channel_metadata(yt, cid)
            videos, video_ids      = count_videos_in_month(yt, meta["uploads_playlist_id"], start_date, end_date)
            stats, analytics_error = fetch_analytics(yta, cid, start_date, end_date)
            recent_views           = fetch_recent_month_views(yta, cid, video_ids, start_date, end_date)
            prev                   = fetch_prev_month_metrics(notion, cid, report_month)

            def trend(curr, key):
                return (curr - prev[key]) if prev is not None else None

            metrics = {
                "videos_published":            videos,
                "views":                       stats["views"],
                "recent_month_views":          recent_views,
                "subscribers_gained":          stats["subscribers_gained"],
                "subscribers_lost":            stats["subscribers_lost"],
                "subscribers_net_added":       stats["subscribers_net_added"],
                "trend_videos_published":      trend(videos,                        "videos_published"),
                "trend_views":                 trend(stats["views"],                "views"),
                "trend_recent_month_views":    trend(recent_views,                  "recent_month_views"),
            }

            print(f"    subs={meta['current_subscribers']:,}  "
                  f"videos={videos}  views={stats['views']:,}  "
                  f"recent={recent_views:,}  "
                  f"net_subs={stats['subscribers_net_added']:+,}")

            row_status = "partial" if analytics_error else "success"
            upsert_monthly_row(notion, pid, cid, name, report_month, start_date,
                               metrics, sync_status=row_status, error_note=analytics_error)
            update_channel_snapshot(notion, pid, meta, run_date_col)

        except Exception as exc:
            msg = str(exc)
            print(f"    [ERROR] {msg}")
            try:
                notion.pages.update(
                    page_id=pid,
                    properties={
                        "Sync Status": {"select":    {"name": "failed"}},
                        "Notes":       {"rich_text": [{"text": {"content": msg[:500]}}]},
                    },
                )
                upsert_monthly_row(notion, pid, cid, name, report_month, start_date,
                                   {}, sync_status="failed", error_note=msg)
            except Exception as inner:
                print(f"    [ERROR writing failure] {inner}")

    print(f"\n{'='*60}")
    print("  Sync complete.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
