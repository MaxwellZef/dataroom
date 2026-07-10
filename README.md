# Dataroom Bot

A personal file catalog you control from Telegram. Your files stay in Google
Drive (no duplicate storage, no size limits to manage) — this app keeps an
index of everything and fetches the actual bytes the moment you ask for
them. Add as many Drive links (single files or whole folders) as you want,
whenever you want, and pull any file back into a chat on demand.

## How it works

- `/start` or `/menu` shows an in-chat button menu with three options —
  **📥 Get**, **➕ Add link**, **🔎 Search**. Tapping a button prompts
  you for whatever it needs (a filename to search for, a Drive link, or
  search text) and walks the rest of the flow through buttons where
  possible, with a **« Back** to bail out along the way.
- The ☰ menu button next to the message box (Telegram's native command
  menu) lists every slash command with a one-line description — tap it
  any time for the full command reference without scrolling back to
  `/start`.
- Tapping **➕ Add link** (or typing `/addlink <drive-url>`) previews a
  file or folder — without saving anything yet — and shows you every
  file it found so you can check it's the right one before committing.
  Company assignment is then done entirely with buttons: "Use the
  suggested name" (folders only, taken from the folder's own name),
  "Existing company" (pick from a list), or "New company name" (type
  one), with **« Back** to discard the preview instead.
- `/companies` lists every company you've filed things under and how
  many files each has, with a **Delete** button per company. Deleting
  a company never deletes its files — they stay in the catalog and
  just become unfiled (no company assigned).
- **🔎 Search** (or `/search`) lands straight on your most recently
  added files, paginated, with buttons to switch to browsing by company
  or searching by filename. Tapping any file shows its details with
  **Get**, **Replace**, and **Delete** buttons right there. `/find
  <text>` is the direct command-line shortcut for a filename search.
- `/get <name>` fetches a file by filename, not catalog id. The exact
  filename resolves straight to that file and downloads it from Drive
  right then (the first send caches Telegram's `file_id`, so the next
  `/get` of the same file is instant). Anything else is treated as a
  search — e.g. `/get KTP` shows every file with "KTP" in the name as
  buttons to tap, even if there's only one match, instead of downloading
  something you didn't explicitly ask for. If nothing contains your text
  at all (e.g. a typo — "KTP joni" instead of "KTP John"), it falls back
  to a "Did you mean" list of the closest-sounding filenames instead of
  just saying nothing was found.
- `/rename <id> <new name>` changes how a file is *displayed* in the
  catalog. It never touches the actual file in Drive, and it isn't
  permanent in the way you might expect: the next time that same file
  gets pulled in again (e.g. re-running `/addlink` on its folder), the
  catalog name resets to whatever Drive reports. Also reachable via the
  **Rename** button in `/search`.
- `/delete <id>` removes an entry from the catalog. This only affects the
  index — the actual file in Google Drive is never touched or deleted.
- `/replace <id> <new drive url>` points an existing catalog entry at a
  different Drive file (e.g. a contract got renewed) while keeping the
  same catalog id, company, and history. Also reachable via the
  **Replace** button in `/search`.
- Only you can use the bot — it's locked to your Telegram user ID.

This only works for Drive files/folders shared as **"Anyone with the
link"**. The app authenticates with a Google API key, not your personal
Google login, so it can only see what that link setting exposes. If
`/addlink` 403s on a particular file, open it in Drive and change its
sharing setting.

## 1. Create the Telegram bot

1. Open a chat with [@BotFather](https://t.me/BotFather) on Telegram.
2. Send `/newbot` and follow the prompts.
3. Copy the token it gives you → this is `TELEGRAM_BOT_TOKEN`.

## 2. Get a Google Drive API key

1. Go to the [Google Cloud Console](https://console.cloud.google.com/),
   create a project (or reuse one).
2. APIs & Services → Library → enable **Google Drive API**.
3. APIs & Services → Credentials → Create Credentials → **API key**.
4. Click into the key and restrict it to the Drive API (recommended, not
   required).
5. Copy it → this is `GOOGLE_API_KEY`.

No OAuth consent screen, no verification, no expiring tokens — this key
only ever reads publicly-shared Drive content.

## 3. Configure

```
cp .env.example .env
```

Fill in `TELEGRAM_BOT_TOKEN` and `GOOGLE_API_KEY`. Leave
`ALLOWED_TELEGRAM_USER_IDS` empty for now.

## 4. Run it once to learn your Telegram user ID

```
pip install -r requirements.txt
python -m app.main
```

Message your bot `/start` on Telegram. It will reply with your numeric
user ID because `ALLOWED_TELEGRAM_USER_IDS` isn't set yet. Copy that ID
into `.env`:

```
ALLOWED_TELEGRAM_USER_IDS=123456789
```

(Comma-separate multiple IDs if more than one person should have access.)
Restart the bot (`Ctrl+C`, then `python -m app.main` again) and it's live.

## 5. Deploy somewhere it stays running

This needs to run continuously (it long-polls Telegram for messages), so
pick a host that keeps a small process alive 24/7:

- **Railway** (recommended): new project → deploy from this repo → it
  detects the `Dockerfile` automatically. Add the env vars from `.env` in
  the Railway dashboard. Railway's usage-based free credit comfortably
  covers a bot this small.
- **Fly.io**: `fly launch` (it'll pick up the `Dockerfile`), then
  `fly secrets set TELEGRAM_BOT_TOKEN=... GOOGLE_API_KEY=... ALLOWED_TELEGRAM_USER_IDS=...`.
- **Render**: `render.yaml` is included, but note Render's *free* web
  services spin down after 15 minutes of inactivity and only wake on an
  inbound HTTP request — Telegram polling won't wake it back up reliably.
  Use Render's paid "Starter" tier for an always-on process, or prefer
  Railway/Fly.io.

By default the catalog is a local SQLite file (`dataroom.db`), which is
fine as long as your host gives you a persistent disk/volume for it. If
it doesn't (e.g. a container that gets recreated on every deploy), point
`DATABASE_URL` at a managed Postgres instead — Railway and Fly.io both
offer one you can attach in a couple of clicks:

```
DATABASE_URL=postgresql+psycopg2://user:password@host:5432/dataroom
```

(Add `psycopg2-binary` to `requirements.txt` if you switch to Postgres.)

## Commands

| Command | What it does |
|---|---|
| `/addlink <drive url>` | Preview a Drive file or folder link — lists what's in it, nothing is saved yet. Company assignment happens via buttons on the preview; « Back discards it. |
| `/companies` | List companies (with how many files each has) and delete one via button |
| `/search` | Recent files, paginated, with buttons to switch to company/filename search |
| `/menu` | Show the button menu (Get / Add link / Search) again |
| `/find <text>` | Search filenames directly from the command line |
| `/get <id or name>` | Fetch a file into the chat |
| `/delete <id>` | Remove a file from the catalog (Drive itself is untouched) |
| `/replace <id> <new drive url>` | Point a catalog entry at a different Drive file |

## Limits worth knowing

- Telegram bots can't upload files bigger than 50MB. For anything larger,
  `/get` replies with a direct Drive link instead of the file itself.
- WhatsApp isn't wired up yet. Meta's WhatsApp Business API requires
  business verification and app review before it'll send/receive
  messages in production, which is a much heavier lift than Telegram's
  instant bot tokens. If you want it later, the Drive/catalog logic in
  `app/catalog.py` and `app/drive.py` is already decoupled from Telegram
  specifics (`app/bot.py`) — a WhatsApp adapter would live alongside
  `app/bot.py` and reuse those two modules.

## Development

```
pip install -r requirements.txt
pytest
```
