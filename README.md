# Dataroom Bot

A personal file catalog you control from Telegram. Your files stay in Google
Drive (no duplicate storage, no size limits to manage) — this app keeps an
index of everything and fetches the actual bytes the moment you ask for
them. Add as many Drive links (single files or whole folders) as you want,
whenever you want, and pull any file back into a chat on demand.

## How it works

- `/start` or `/menu` shows a persistent bottom keyboard with three
  buttons — **📥 Get**, **➕ Add link**, **🔎 Find** — plus a **« Back**
  that's always there to bail out of whatever you're doing. Tapping a
  button prompts you for whatever it needs (a Drive link, a catalog
  id/filename, or search text) and walks the rest of the flow through
  buttons where possible.
- Tapping **➕ Add link** (or typing `/addlink <drive-url>`) previews a
  file or folder — without saving anything yet — and shows you every
  file it found so you can check it's the right one before committing.
  Company assignment is then done with buttons: "Use the suggested
  name" (folders only, taken from the folder's own name), "Existing
  company" (pick from a list), or "New company name" (type one). Typing
  `/confirm`, `/confirm <number>`, or `/confirm new <name>` still works
  too. `/cancel` discards the preview instead.
- `/companies` lists every company you've filed things under and how
  many files each has.
- `/search` opens a tappable menu — browse by company, by filename, or
  recent — instead of typing commands. Tapping a file shows its details
  with **Get**, **Replace**, and **Delete** buttons right there.
- `/list`, `/find <text>` are the command-line equivalent: browse/search
  the whole catalog as text (each entry shows which company it's filed
  under, if any).
- `/get <id-or-name>` downloads the file from Drive right then and sends it
  to you in the chat. The first time a file is sent, Telegram hands back a
  `file_id`; the bot caches that so the next `/get` is instant instead of
  re-downloading from Drive.
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
| `/addlink <drive url>` | Preview a Drive file or folder link — lists what's in it, nothing is saved yet |
| `/confirm` | File the last preview under its suggested company (folders only) |
| `/confirm <number>` | File the last preview under an existing company from the list |
| `/confirm new <name>` | File the last preview under a brand-new company name |
| `/cancel` | Discard the last preview instead of filing it |
| `/companies` | List companies and how many files are under each |
| `/search` | Open the tappable browse/search menu (company, filename, recent) |
| `/menu` | Show the bottom button keyboard (Get / Add link / Find / Back) again |
| `/list [page]` | Browse the catalog, 20 at a time |
| `/find <text>` | Search filenames |
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
