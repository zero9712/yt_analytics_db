# 📊 YouTube Analytics → Notion DB

Automatically pulls previous-month YouTube metrics for all owned channels and upserts them into two Notion databases — runs weekly via GitHub Actions.

---

## How it works

```
Every Monday 09:00 KST
        │
        ▼
  Read active channels          ← Notion: YT Channels
        │
        ▼
  channels.list                 ← YouTube Data API v3
  (metadata + uploads playlist)
        │
        ▼
  playlistItems.list            ← YouTube Data API v3
  (count uploads in report month, Pacific Time)
        │
        ▼
  reports.query                 ← YouTube Analytics API
  (views, subscribersGained, subscribersLost)
        │
        ▼
  Upsert monthly row            ← Notion: YT Monthly Metrics
  Update channel snapshot       ← Notion: YT Channels
```

**Report month logic:** running on any day in March 2026 → targets `2026-02` (Feb 1–28, Pacific Time). YouTube Analytics uses Pacific Time month boundaries, so all date math is done in PT for consistency.

---

## Notion databases

| Database | Data Source ID |
|---|---|
| **YT Channels** | `49d9ea76-ad99-4afe-8469-b7b0854297d5` |
| **YT Monthly Metrics** | `2eab0929-173c-4b9a-a780-b7764a30b4b3` |

### YT Channels schema
Tracks current state: one row per channel.

| Property | Type | Notes |
|---|---|---|
| Name | Title | Channel name |
| Channel ID | Rich text | Stable `UC…` ID — primary key |
| Handle | Rich text | `@handle` if available |
| Channel URL | URL | YouTube link |
| Uploads Playlist ID | Rich text | Auto-filled on first sync |
| Current Subscribers | Number | Snapshot — updated every run |
| Hidden Subscriber Count | Checkbox | From Data API |
| Active | Checkbox | Uncheck to pause syncing |
| Analytics Access | Select | `owned` / `content_owner` / `unknown` / `public_only` |
| Last Synced At | Date | Timestamp of last successful run |
| Sync Status | Select | 🟢 success / 🟡 partial / 🔴 failed |
| Notes | Rich text | Error details if failed |

### YT Monthly Metrics schema
One row per channel per month — never overwritten, only patched.

| Property | Type | Notes |
|---|---|---|
| Name | Title | `YYYY-MM \| Channel Name` |
| Channel | Relation | → YT Channels |
| Channel ID | Rich text | Duplicated for easy filtering |
| Report Month | Rich text | `YYYY-MM` e.g. `2026-02` |
| Month Start | Date | First day of month |
| Videos Uploaded | Number | Count of uploads (Pacific Time) |
| Views | Number | Monthly channel views |
| Subscribers Gained | Number | Analytics metric |
| Subscribers Lost | Number | Analytics metric |
| Subscribers Net Added | Number | `gained − lost` |
| Last Synced At | Date | Last upsert time |
| Sync Status | Select | 🟢 success / 🟡 partial / 🔴 failed |
| Error Note | Rich text | API/auth error details |

---

## Local setup

### 1. Clone and install

```bash
git clone https://github.com/zero9712/yt_analytics_db.git
cd yt_analytics_db
pip install -r requirements.txt
```

### 2. Google Cloud

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create or select a project
2. Enable these two APIs:
   - **YouTube Data API v3**
   - **YouTube Analytics API**
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
4. Download JSON → save as `client_secret.json` in the project root
5. **OAuth consent screen** → add your Google account as a **Test user**

### 3. Notion integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) → **New integration**
2. Copy the **Internal Integration Token**
3. In Notion, open each database → **⋯ → Connections → Connect** your integration:
   - YT Channels
   - YT Monthly Metrics

### 4. Environment

```bash
cp .env.example .env
# Open .env and paste your NOTION_TOKEN
```

### 5. First run

```bash
python yt_notion_sync.py
```

A browser window opens for Google OAuth consent. After approval, `token.json` is saved locally and all future runs are fully headless.

---

## GitHub Actions (automated weekly sync)

### One-time secrets setup

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Where to get it |
|---|---|
| `NOTION_TOKEN` | Notion integrations page |
| `GOOGLE_CLIENT_SECRET_JSON` | Paste full contents of `client_secret.json` |
| `GOOGLE_TOKEN_JSON` | Paste full contents of `token.json` (run locally first) |

The workflow also needs write permission to update the token secret automatically.  
Go to **Settings → Actions → General → Workflow permissions** → enable **Read and write permissions**.

### Schedule

The workflow (`.github/workflows/yt_sync.yml`) runs:
- **Every Monday at 09:00 KST** (00:00 UTC) — automatic
- **On demand** — go to Actions tab → *YouTube → Notion Sync* → **Run workflow**

`token.json` is written back to the `GOOGLE_TOKEN_JSON` secret after each run so credentials stay refreshed automatically — no manual intervention needed.

---

## Project structure

```
yt_analytics_db/
├── yt_notion_sync.py            # Main sync script
├── requirements.txt             # Python dependencies
├── .env.example                 # Environment variable template
├── .gitignore                   # Excludes .env, secrets, token
├── .github/
│   └── workflows/
│       └── yt_sync.yml          # GitHub Actions weekly schedule
└── README.md
```

---

## Adding or removing channels

Open the **YT Channels** database in Notion:
- **Add a channel:** create a new row, fill in `Channel ID` and set `Active = ✓`. The next sync will populate all other fields automatically.
- **Pause a channel:** uncheck `Active`. It will be skipped on all future runs.
- `Channel ID` is the stable `UC…` identifier from the YouTube channel URL.

---

## Quota usage

Each run consumes approximately:

| API call | Cost | Per channel |
|---|---|---|
| `channels.list` | 1 unit | ×N channels |
| `playlistItems.list` | 1 unit/page | ×pages of uploads |
| `reports.query` | 1 unit | ×N channels |

Default YouTube Data API quota is **10,000 units/day**. With 24 channels and typical upload volumes, each weekly run uses well under 500 units.
