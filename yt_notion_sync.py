"""
yt_notion_sync.py
─────────────────────────────────────────────────────────────────────────────
Pulls YouTube Data API + Analytics data for every active channel in the
Notion "YT Channels" database and upserts a monthly-metrics row into
"YT Monthly Metrics".

SETUP
─────
1.  Install dependencies:
        pip install google-auth google-auth-oauthlib google-api-python-client \
                    notion-client python-dateutil pytz python-dotenv

2.  Copy .env.example → .env and fill in NOTION_TOKEN.

3.  Place client_secret.json (Google OAuth credentials) in the project root.
    Enable in Google Cloud Console:
      • YouTube Data API v3
      • YouTube Analytics API

4.  On first run the script opens a browser for OAuth consent and caches
    token.json.  Subsequent runs are fully headless.
"""

import os
import datetime
import pytz
from dotenv import load_dotenv

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from notion_client import Client

# ── Load .env ─────────────────────────────────────────────────────────────────
load_dotenv()

NOTION_TOKEN              = os.environ["NOTION_TOKEN"]
GOOGLE_CLIENT_SECRET_PATH = os.environ.get("GOOGLE_CLIENT_SECRET_PATH", "client_secret.json")
GOOGLE_TOKEN_PATH         = os.environ.get("GOOGLE_TOKEN_PATH", "token.json")

# ── Notion database IDs (pre-configured) ──────────────────────────────────────
YT_CHANNELS_DS_ID = "49d9ea76-ad99-4afe-8469-b7b0854297d5"
YT_MONTHLY_DS_ID  = "2eab0929-173c-4b9a-a780-b7764a30b4b3"

# ── Google OAuth scopes ───────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

PACIFIC = pytz.timezone("America/Los_Angeles")


# ─────────────────────────────────────────────────────────────────────────────
# 1.  GOOGLE AUTH
# ─────────────────────────────────────────────────────────────────────────────

def get_google_credentials() -> Credentials:
    """Load cached OAuth token or run the browser consent flow."""
    creds = None
    if os.path.exists(GOOGLE_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_CLIENT_SECRET_PATH, SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GOOGLE_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


# ─────────────────────────────────────────────────────────────────────────────
# 2.  REPORT MONTH  (previous calendar month, Pacific Time)
# ─────────────────────────────────────────────────────────────────────────────

def get_report_month() -> tuple[str, str, str]:
    """
    Returns (report_month, start_date, end_date) for the previous calendar month.
    e.g. running on 2026-03-30  →  ('2026-02', '2026-02-01', '2026-02-28')
    """
    today_pt          = datetime.datetime.now(PACIFIC).date()
    first_of_month    = today_pt.replace(day=1)
    last_month_end    = first_of_month - datetime.timedelta(days=1)
    last_month_start  = last_month_end.replace(day=1)

    return (
        last_month_start.strftime("%Y-%m"),
        last_month_start.isoformat(),
        last_month_end.isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3.  NOTION  —  read active channels
# ─────────────────────────────────────────────────────────────────────────────

def get_active_channels(notion: Client) -> list[dict]:
    """Query YT Channels for all rows where Active = true."""
    channels, cursor = [], None
    while True:
        kwargs = {
            "database_id": YT_CHANNELS_DS_ID,
            "filter": {"property": "Active", "checkbox": {"equals": True}},
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor

        resp = notion.databases.query(**kwargs)

        for page in resp.get("results", []):
            props      = page.get("properties", {})
            name       = (props.get("Name",       {}).get("title",     [{}])[0]
                          .get("plain_text", ""))
            channel_id = (props.get("Channel ID", {}).get("rich_text", [{}])[0]
                          .get("plain_text", ""))
            if channel_id:
                channels.append({"page_id": page["id"], "name": name,
                                  "channel_id": channel_id})

        if resp.get("has_more"):
            cursor = resp["next_cursor"]
        else:
            break

    print(f"[notion] {len(channels)} active channels found.")
    return channels


# ─────────────────────────────────────────────────────────────────────────────
# 4.  YOUTUBE DATA API  —  channel metadata
# ─────────────────────────────────────────────────────────────────────────────

def fetch_channel_metadata(yt, channel_id: str) -> dict:
    """channels.list — 1 quota unit."""
    resp  = yt.channels().list(
        part="snippet,statistics,contentDetails", id=channel_id
    ).execute()
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


# ─────────────────────────────────────────────────────────────────────────────
# 5.  YOUTUBE DATA API  —  count uploads in report month
# ─────────────────────────────────────────────────────────────────────────────

def count_videos_in_month(yt, uploads_playlist_id: str,
                           start_date: str, end_date: str) -> int:
    """
    Pages through playlistItems.list (1 quota unit / 50 items).
    Stops early once videos fall before start_date (playlist is newest-first).
    """
    start_pt   = PACIFIC.localize(datetime.datetime.fromisoformat(start_date))
    end_pt     = PACIFIC.localize(
        datetime.datetime.fromisoformat(end_date) + datetime.timedelta(days=1)
    )
    count, page_token, stop_early = 0, None, False

    while not stop_early:
        kwargs = {"part": "contentDetails", "playlistId": uploads_playlist_id,
                  "maxResults": 50}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = yt.playlistItems().list(**kwargs).execute()

        for item in resp.get("items", []):
            raw = item["contentDetails"].get("videoPublishedAt", "")
            if not raw:
                continue
            pub_pt = datetime.datetime.fromisoformat(
                raw.replace("Z", "+00:00")
            ).astimezone(PACIFIC)

            if pub_pt < start_pt:
                stop_early = True
                break
            if pub_pt < end_pt:
                count += 1

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return count


# ─────────────────────────────────────────────────────────────────────────────
# 6.  YOUTUBE ANALYTICS API  —  monthly metrics
# ─────────────────────────────────────────────────────────────────────────────

def fetch_analytics(yta, channel_id: str,
                    start_date: str, end_date: str) -> dict:
    """reports.query — views + subscriber flow for the report month."""
    resp = yta.reports().query(
        ids=f"channel=={channel_id}",
        startDate=start_date,
        endDate=end_date,
        metrics="views,subscribersGained,subscribersLost",
    ).execute()

    rows = resp.get("rows", [])
    if not rows:
        print("    [analytics] No data rows — month may not be finalised yet.")
        return {"views": 0, "subscribers_gained": 0,
                "subscribers_lost": 0, "subscribers_net_added": 0}

    views, gained, lost = int(rows[0][0]), int(rows[0][1]), int(rows[0][2])
    return {"views": views, "subscribers_gained": gained,
            "subscribers_lost": lost, "subscribers_net_added": gained - lost}


# ─────────────────────────────────────────────────────────────────────────────
# 7.  NOTION  —  upsert monthly metrics row
# ─────────────────────────────────────────────────────────────────────────────

def upsert_monthly_row(notion: Client, channel_page_id: str, channel_id: str,
                       channel_name: str, report_month: str, month_start: str,
                       metrics: dict, sync_status: str = "success",
                       error_note: str = "") -> None:
    now_iso    = datetime.datetime.now(datetime.timezone.utc).isoformat()
    properties = {
        "Name":       {"title":     [{"text": {"content": f"{report_month} | {channel_name}"}}]},
        "Channel":    {"relation":  [{"id": channel_page_id}]},
        "Channel ID": {"rich_text": [{"text": {"content": channel_id}}]},
        "Report Month": {"rich_text": [{"text": {"content": report_month}}]},
        "Month Start":  {"date": {"start": month_start}},
        "Videos Uploaded":       {"number": metrics.get("videos_uploaded", 0)},
        "Views":                 {"number": metrics.get("views", 0)},
        "Subscribers Gained":    {"number": metrics.get("subscribers_gained", 0)},
        "Subscribers Lost":      {"number": metrics.get("subscribers_lost", 0)},
        "Subscribers Net Added": {"number": metrics.get("subscribers_net_added", 0)},
        "Last Synced At": {"date": {"start": now_iso}},
        "Sync Status":    {"select": {"name": sync_status}},
    }
    if error_note:
        properties["Error Note"] = {"rich_text": [{"text": {"content": error_note[:500]}}]}

    existing = notion.databases.query(
        database_id=YT_MONTHLY_DS_ID,
        filter={"and": [
            {"property": "Channel ID",   "rich_text": {"equals": channel_id}},
            {"property": "Report Month", "rich_text": {"equals": report_month}},
        ]},
        page_size=1,
    )

    if existing.get("results"):
        notion.pages.update(page_id=existing["results"][0]["id"],
                            properties=properties)
        print(f"    [notion] Updated  →  {report_month} | {channel_name}")
    else:
        notion.pages.create(parent={"database_id": YT_MONTHLY_DS_ID},
                            properties=properties)
        print(f"    [notion] Created  →  {report_month} | {channel_name}")


# ─────────────────────────────────────────────────────────────────────────────
# 8.  NOTION  —  update channel snapshot
# ─────────────────────────────────────────────────────────────────────────────

def update_channel_snapshot(notion: Client, page_id: str, metadata: dict) -> None:
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    notion.pages.update(
        page_id=page_id,
        properties={
            "Uploads Playlist ID":   {"rich_text": [{"text": {"content": metadata["uploads_playlist_id"]}}]},
            "Current Subscribers":   {"number":    metadata["current_subscribers"]},
            "Hidden Subscriber Count": {"checkbox": metadata["hidden_subscriber_count"]},
            "Last Synced At":        {"date": {"start": now_iso}},
            "Sync Status":           {"select": {"name": "success"}},
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# 9.  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    report_month, start_date, end_date = get_report_month()
    print(f"\n{'='*60}")
    print(f"  Report month : {report_month}  ({start_date} → {end_date}, PT)")
    print(f"{'='*60}\n")

    creds  = get_google_credentials()
    yt     = build("youtube",          "v3", credentials=creds)
    yta    = build("youtubeAnalytics", "v2", credentials=creds)
    notion = Client(auth=NOTION_TOKEN)

    channels = get_active_channels(notion)

    for ch in channels:
        cid, name, pid = ch["channel_id"], ch["name"], ch["page_id"]
        print(f"\n── {name}  ({cid})")
        try:
            meta    = fetch_channel_metadata(yt, cid)
            videos  = count_videos_in_month(yt, meta["uploads_playlist_id"],
                                            start_date, end_date)
            stats   = fetch_analytics(yta, cid, start_date, end_date)

            print(f"    subs={meta['current_subscribers']:,}  "
                  f"videos={videos}  views={stats['views']:,}  "
                  f"net_subs={stats['subscribers_net_added']:+,}")

            upsert_monthly_row(notion, pid, cid, name, report_month,
                               start_date, {"videos_uploaded": videos, **stats})
            update_channel_snapshot(notion, pid, meta)

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
                upsert_monthly_row(notion, pid, cid, name, report_month,
                                   start_date, {}, sync_status="failed",
                                   error_note=msg)
            except Exception as inner:
                print(f"    [ERROR writing failure] {inner}")

    print(f"\n{'='*60}")
    print("  Sync complete.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
