from __future__ import annotations

import asyncio
import functools
import io
import logging

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.catalog import (
    LinkPreview,
    commit_import,
    delete_file,
    files_by_company,
    find_file,
    get_file,
    get_or_create_company,
    list_companies,
    list_files,
    preview_link,
    record_telegram_file_id,
    replace_file_source,
    search_files,
)
from app.config import ALLOWED_TELEGRAM_USER_IDS, TELEGRAM_MAX_FILE_BYTES, TELEGRAM_BOT_TOKEN
from app.db import SessionLocal
from app.drive import DriveClient, DriveFile, DriveLinkError, extract_drive_id
from app.models import Company, File

logger = logging.getLogger(__name__)

MAX_PREVIEW_LINES = 40
TELEGRAM_MESSAGE_LIMIT = 3800
MENU_PAGE_SIZE = 8

GET_LABEL = "📥 Get"
ADD_LABEL = "➕ Add link"
SEARCH_LABEL = "🔎 Search"
BACK_LABEL = "« Back"

MAIN_MENU_TEXT = (
    "👋 Welcome to Dataroom Bot!\n\n"
    "Manage and organize your Google Drive files directly from Telegram.\n\n"
    "🚀 Quick Start\n\n"
    "1️⃣ Add a Google Drive link — /addlink\n"
    "2️⃣ Browse or search your files — /search\n\n"
    "📋 Other Commands\n\n"
    "📁 View companies — /companies\n"
    "🔎 Find a file — /find\n"
    "📥 Get a file — /get\n"
    "♻️ Replace a file — /replace\n"
    "🗑️ Remove from catalog — /delete (Drive file stays untouched)\n"
    "❌ Cancel current preview — /cancel"
)


def _human_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "?"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _truncate(text: str, limit: int = 45) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _send_chunked(message, text: str, reply_markup=None) -> None:
    chunks = [text[i : i + TELEGRAM_MESSAGE_LIMIT] for i in range(0, len(text), TELEGRAM_MESSAGE_LIMIT)] or [""]
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await message.reply_text(chunk, reply_markup=reply_markup if is_last else None)


def owner_only(handler):
    @functools.wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id if user else None
        if ALLOWED_TELEGRAM_USER_IDS and user_id in ALLOWED_TELEGRAM_USER_IDS:
            return await handler(update, context)

        message = update.effective_message
        if not ALLOWED_TELEGRAM_USER_IDS:
            if message:
                await message.reply_text(
                    "This bot isn't locked to an owner yet.\n"
                    f"Your Telegram user ID is {user_id}.\n"
                    "Add it to ALLOWED_TELEGRAM_USER_IDS and restart the bot to use it."
                )
            return

        if message:
            await message.reply_text("Not authorized.")

    return wrapped


# --- small DB-thread helpers shared by commands and the menus ---


async def _fetch_companies() -> list[tuple]:
    def _run():
        session = SessionLocal()
        try:
            return list_companies(session)
        finally:
            session.close()

    return await asyncio.to_thread(_run)


async def _fetch_files_by_company(company_id: int, page: int) -> tuple[list[File], int]:
    def _run():
        session = SessionLocal()
        try:
            return files_by_company(session, company_id, page=page, page_size=MENU_PAGE_SIZE)
        finally:
            session.close()

    return await asyncio.to_thread(_run)


async def _fetch_recent(page: int) -> tuple[list[File], int]:
    def _run():
        session = SessionLocal()
        try:
            return list_files(session, page=page, page_size=MENU_PAGE_SIZE)
        finally:
            session.close()

    return await asyncio.to_thread(_run)


async def _fetch_search(query_text: str) -> list[File]:
    def _run():
        session = SessionLocal()
        try:
            return search_files(session, query_text)
        finally:
            session.close()

    return await asyncio.to_thread(_run)


async def _fetch_file(file_id: int) -> File | None:
    def _run():
        session = SessionLocal()
        try:
            return get_file(session, file_id)
        finally:
            session.close()

    return await asyncio.to_thread(_run)


async def _lookup_file(identifier: str) -> File | None:
    def _run():
        session = SessionLocal()
        try:
            return find_file(session, identifier)
        finally:
            session.close()

    return await asyncio.to_thread(_run)


async def _do_delete(file_id: int) -> File | None:
    def _run():
        session = SessionLocal()
        try:
            return delete_file(session, file_id)
        finally:
            session.close()

    return await asyncio.to_thread(_run)


async def _do_replace(file_id: int, url: str) -> tuple[File | None, str | None]:
    def _run():
        session = SessionLocal()
        try:
            drive_file = DriveClient().get_metadata(extract_drive_id(url))
            return replace_file_source(session, file_id, drive_file)
        finally:
            session.close()

    try:
        return await asyncio.to_thread(_run)
    except DriveLinkError as exc:
        return None, str(exc)
    except Exception:
        logger.exception("Failed to replace file %s", file_id)
        return None, "Couldn't read that link. Make sure it's shared as \"Anyone with the link\"."


async def _commit_preview_with_company_name(
    preview: LinkPreview, company_name: str
) -> tuple[tuple[str, int, int] | None, str | None]:
    def _run():
        session = SessionLocal()
        try:
            company = get_or_create_company(session, company_name)
            result = commit_import(session, preview, company)
            return (company.name, result.added, result.updated), None
        finally:
            session.close()

    return await asyncio.to_thread(_run)


async def _commit_preview_with_company_id(
    preview: LinkPreview, company_id: int
) -> tuple[tuple[str, int, int] | None, str | None]:
    def _run():
        session = SessionLocal()
        try:
            company = session.get(Company, company_id)
            if company is None:
                return None, "That company no longer exists."
            result = commit_import(session, preview, company)
            return (company.name, result.added, result.updated), None
        finally:
            session.close()

    return await asyncio.to_thread(_run)


async def _deliver_file(message, file_id: int) -> None:
    """Send a catalogued file into the chat, from Telegram's cache or straight from Drive."""

    def _get_session_and_row():
        session = SessionLocal()
        try:
            return session, get_file(session, file_id)
        except Exception:
            session.close()
            raise

    try:
        session, row = await asyncio.to_thread(_get_session_and_row)
    except Exception:
        logger.exception("Failed to look up file %s", file_id)
        await message.reply_text("Something went wrong looking that up. Try again.")
        return

    try:
        if row is None:
            await message.reply_text("That file isn't in the catalog anymore.")
            return

        if row.telegram_file_id:
            await message.reply_document(document=row.telegram_file_id, filename=row.name)
            await message.reply_text(MAIN_MENU_TEXT, reply_markup=_main_menu_keyboard())
            return

        if row.size_bytes and row.size_bytes > TELEGRAM_MAX_FILE_BYTES:
            link = row.web_view_link or f"https://drive.google.com/file/d/{row.drive_file_id}/view"
            await message.reply_text(
                f"{row.name} is {_human_size(row.size_bytes)}, too big for Telegram to deliver. "
                f"Here's the Drive link instead: {link}"
            )
            return

        await message.reply_text(f"Fetching {row.name} from Drive...")

        def _download():
            drive_file = DriveFile(
                id=row.drive_file_id,
                name=row.name,
                mime_type=row.mime_type,
                size=row.size_bytes,
                web_view_link=row.web_view_link,
            )
            return DriveClient().download_bytes(drive_file)

        try:
            content, filename = await asyncio.to_thread(_download)
        except Exception:
            logger.exception("Failed to download %s", row.drive_file_id)
            await message.reply_text("Couldn't download that file from Drive. Try again later.")
            return

        sent = await message.reply_document(document=InputFile(io.BytesIO(content), filename=filename))
        if sent.document:
            await asyncio.to_thread(record_telegram_file_id, session, row, sent.document.file_id)
        await message.reply_text(MAIN_MENU_TEXT, reply_markup=_main_menu_keyboard())
    finally:
        session.close()


# --- main menu (inline buttons attached to a chat message) ---


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(GET_LABEL, callback_data="mn:get"),
                InlineKeyboardButton(ADD_LABEL, callback_data="mn:add"),
                InlineKeyboardButton(SEARCH_LABEL, callback_data="mn:search"),
            ],
        ]
    )


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(BACK_LABEL, callback_data="mn")]])


# --- inline keyboards for the /search menu ---


def _file_button(row: File) -> InlineKeyboardButton:
    return InlineKeyboardButton(_truncate(row.name), callback_data=f"sd:{row.id}")


def _back_to_search_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Search", callback_data="sm")]])


def _search_home_keyboard(files: list[File], page: int, total: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (total + MENU_PAGE_SIZE - 1) // MENU_PAGE_SIZE)
    rows = [[_file_button(row)] for row in files]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("« Prev", callback_data=f"sr:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next »", callback_data=f"sr:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton("🏢 By company", callback_data="sc:1"),
            InlineKeyboardButton("🔤 By filename", callback_data="sn"),
        ]
    )
    rows.append([InlineKeyboardButton("« Menu", callback_data="mn")])
    return InlineKeyboardMarkup(rows)


async def _search_home_view(page: int) -> tuple[str, InlineKeyboardMarkup]:
    """The Search entry point: recently added files, with company/filename search alongside."""

    rows, total = await _fetch_recent(page)
    if not rows:
        return "No files yet. Use + Add link to add some.", _main_menu_keyboard()
    return f"Recent files ({total}):", _search_home_keyboard(rows, page, total)


def _companies_keyboard(companies: list[tuple], page: int) -> InlineKeyboardMarkup:
    start = (page - 1) * MENU_PAGE_SIZE
    page_items = companies[start : start + MENU_PAGE_SIZE]
    total_pages = max(1, (len(companies) + MENU_PAGE_SIZE - 1) // MENU_PAGE_SIZE)

    rows = [
        [InlineKeyboardButton(f"{company.name} ({count})", callback_data=f"sf:{company.id}:1")]
        for company, count in page_items
    ]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("« Prev", callback_data=f"sc:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next »", callback_data=f"sc:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("« Menu", callback_data="sm")])
    return InlineKeyboardMarkup(rows)


def _paged_files_keyboard(
    files: list[File], page: int, total: int, page_prefix: str, back_callback: str
) -> InlineKeyboardMarkup:
    total_pages = max(1, (total + MENU_PAGE_SIZE - 1) // MENU_PAGE_SIZE)
    rows = [[_file_button(row)] for row in files]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("« Prev", callback_data=f"{page_prefix}{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next »", callback_data=f"{page_prefix}{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("« Back", callback_data=back_callback)])
    return InlineKeyboardMarkup(rows)


def _detail_keyboard(file_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬇️ Get", callback_data=f"sg:{file_id}")],
            [InlineKeyboardButton("🔁 Replace", callback_data=f"sp:{file_id}")],
            [InlineKeyboardButton("🗑 Delete", callback_data=f"sx:{file_id}")],
            [InlineKeyboardButton("« Menu", callback_data="sm")],
        ]
    )


def _delete_confirm_keyboard(file_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes, delete", callback_data=f"sxy:{file_id}"),
                InlineKeyboardButton("✖️ Cancel", callback_data=f"sxn:{file_id}"),
            ]
        ]
    )


def _file_detail_text(row: File) -> str:
    lines = [row.name, f"Size: {_human_size(row.size_bytes)}"]
    lines.append(f"Company: {row.company.name if row.company else '(none)'}")
    lines.append(f"Catalog id: {row.id}")
    return "\n".join(lines)


async def _render_detail(query, file_id: int) -> None:
    row = await _fetch_file(file_id)
    if row is None:
        await query.edit_message_text("That file is gone.", reply_markup=_back_to_search_keyboard())
        return
    await query.edit_message_text(_file_detail_text(row), reply_markup=_detail_keyboard(file_id))


# --- inline keyboards for confirming an /addlink preview ---


def _confirm_keyboard(preview: LinkPreview) -> InlineKeyboardMarkup:
    rows = []
    if preview.suggested_company:
        rows.append(
            [InlineKeyboardButton(f'✅ Use "{_truncate(preview.suggested_company, 35)}"', callback_data="ic:suggested")]
        )
    rows.append([InlineKeyboardButton("🏢 Existing company", callback_data="ic:pick:1")])
    rows.append([InlineKeyboardButton("🆕 New company name", callback_data="ic:new")])
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data="ic:cancel")])
    return InlineKeyboardMarkup(rows)


def _pick_company_keyboard(companies: list[tuple], page: int) -> InlineKeyboardMarkup:
    start = (page - 1) * MENU_PAGE_SIZE
    page_items = companies[start : start + MENU_PAGE_SIZE]
    total_pages = max(1, (len(companies) + MENU_PAGE_SIZE - 1) // MENU_PAGE_SIZE)

    rows = [
        [InlineKeyboardButton(f"{company.name} ({count})", callback_data=f"ic:pickc:{company.id}")]
        for company, count in page_items
    ]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("« Prev", callback_data=f"ic:pick:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next »", callback_data=f"ic:pick:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data="ic:cancel")])
    return InlineKeyboardMarkup(rows)


# --- commands ---


@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MAIN_MENU_TEXT, reply_markup=_main_menu_keyboard())


async def _preview_and_reply(message, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    """Preview a Drive link and reply with what was found. Shared by /addlink and the Add link button."""

    await message.reply_text("Reading that link from Drive...")

    def _run():
        return preview_link(url, DriveClient())

    try:
        preview = await asyncio.to_thread(_run)
    except DriveLinkError as exc:
        await message.reply_text(str(exc), reply_markup=_main_menu_keyboard())
        return
    except Exception:
        logger.exception("Failed to preview link %s", url)
        await message.reply_text(
            "Couldn't read that link. Make sure it's shared as "
            "\"Anyone with the link\" and try again.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    if not preview.files:
        await message.reply_text("That link resolved but no files were found in it.", reply_markup=_main_menu_keyboard())
        return

    context.user_data["pending_import"] = preview

    lines = [f"Found {len(preview.files)} file(s) in \"{preview.root_name}\":"]
    lines += [f"- {f.name}" for f in preview.files[:MAX_PREVIEW_LINES]]
    if len(preview.files) > MAX_PREVIEW_LINES:
        lines.append(f"...and {len(preview.files) - MAX_PREVIEW_LINES} more")
    lines.append("")
    lines.append("Pick a company below to file these under.")

    await _send_chunked(message, "\n".join(lines), reply_markup=_confirm_keyboard(preview))


@owner_only
async def addlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        context.user_data["awaiting_addlink_url"] = True
        await update.message.reply_text(
            "Send the Google Drive link (file or folder) you want to add.", reply_markup=_back_keyboard()
        )
        return

    await _preview_and_reply(update.message, context, context.args[0])


@owner_only
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    had_pending = context.user_data.pop("pending_import", None) is not None
    for key in (
        "awaiting_addlink_url",
        "awaiting_get_query",
        "awaiting_search_text",
        "awaiting_new_company_name",
        "pending_replace_file_id",
    ):
        context.user_data.pop(key, None)

    await update.message.reply_text(
        "Discarded." if had_pending else "Nothing pending.",
        reply_markup=_main_menu_keyboard(),
    )


@owner_only
async def companies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    companies = await _fetch_companies()
    if not companies:
        await update.message.reply_text("No companies yet. Add one via /addlink and pick a company.")
        return

    lines = [f"{i}. {company.name} ({count} files)" for i, (company, count) in enumerate(companies, start=1)]
    await _send_chunked(update.message, "\n".join(lines))


def _format_file_line(row: File) -> str:
    line = f"[{row.id}] {row.name} ({_human_size(row.size_bytes)})"
    if row.company:
        line += f" — {row.company.name}"
    return line


@owner_only
async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        context.user_data["awaiting_search_text"] = True
        await update.message.reply_text(
            "Send the text you want to search filenames for.", reply_markup=_back_keyboard()
        )
        return

    rows = await _fetch_search(" ".join(context.args))
    if not rows:
        await update.message.reply_text("No matches.")
        return

    lines = [_format_file_line(row) for row in rows]
    await _send_chunked(update.message, "\n".join(lines))


@owner_only
async def get_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        context.user_data["awaiting_get_query"] = True
        await update.message.reply_text(
            "Send the catalog id or filename of the file you want.", reply_markup=_back_keyboard()
        )
        return

    identifier = " ".join(context.args)
    try:
        row = await _lookup_file(identifier)
    except Exception:
        logger.exception("Failed to look up %s", identifier)
        await update.message.reply_text("Something went wrong looking that up. Try again.")
        return

    if row is None:
        await update.message.reply_text(f"No file matching {identifier!r}.")
        return

    await _deliver_file(update.message, row.id)


@owner_only
async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /delete <catalog id> (see /search or /find for ids)")
        return

    row = await _do_delete(int(context.args[0]))
    if row is None:
        await update.message.reply_text(f"No file with id {context.args[0]}.")
        return
    await update.message.reply_text(f'Deleted "{row.name}" from the catalog (the Drive file is untouched).')


@owner_only
async def replace_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /replace <catalog id> <new drive url>")
        return

    row, error = await _do_replace(int(context.args[0]), context.args[1])
    if error:
        await update.message.reply_text(error)
        return
    await update.message.reply_text(f'Replaced. "{row.name}" now points at the new Drive file.')


@owner_only
async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = await _search_home_view(1)
    await update.message.reply_text(text, reply_markup=keyboard)


@owner_only
async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MAIN_MENU_TEXT, reply_markup=_main_menu_keyboard())


# --- inline menu callback + free-text follow-ups ---


@owner_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "mn":
        for key in (
            "pending_import",
            "pending_replace_file_id",
            "awaiting_addlink_url",
            "awaiting_get_query",
            "awaiting_search_text",
            "awaiting_new_company_name",
        ):
            context.user_data.pop(key, None)
        await query.edit_message_text(MAIN_MENU_TEXT, reply_markup=_main_menu_keyboard())
        return

    if data == "mn:get":
        context.user_data["awaiting_get_query"] = True
        await query.edit_message_text(
            "Send the catalog id or filename of the file you want.", reply_markup=_back_keyboard()
        )
        return

    if data == "mn:add":
        context.user_data["awaiting_addlink_url"] = True
        await query.edit_message_text(
            "Send the Google Drive link (file or folder) you want to add.", reply_markup=_back_keyboard()
        )
        return

    if data == "mn:search":
        text, keyboard = await _search_home_view(1)
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if data == "ic:suggested":
        preview: LinkPreview | None = context.user_data.get("pending_import")
        if preview is None or not preview.suggested_company:
            await query.edit_message_text("Nothing pending.")
            return
        payload, error = await _commit_preview_with_company_name(preview, preview.suggested_company)
        if error:
            await query.edit_message_text(error)
            return
        context.user_data.pop("pending_import", None)
        company_name, added, updated = payload
        await query.edit_message_text(
            f'Filed under "{company_name}": added {added} file(s), refreshed {updated} already catalogued.'
        )
        return

    if data == "ic:new":
        if context.user_data.get("pending_import") is None:
            await query.edit_message_text("Nothing pending.")
            return
        context.user_data["awaiting_new_company_name"] = True
        await query.edit_message_text("Send the company name to file these under.")
        await query.message.reply_text(
            "(Type the name, or tap « Back below to cancel.)", reply_markup=_back_keyboard()
        )
        return

    if data.startswith("ic:pick:"):
        if context.user_data.get("pending_import") is None:
            await query.edit_message_text("Nothing pending.")
            return
        page = int(data.split(":")[2])
        companies = await _fetch_companies()
        if not companies:
            await query.edit_message_text(
                "No existing companies yet. Use \"New company name\" instead.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🆕 New company name", callback_data="ic:new")],
                        [InlineKeyboardButton("✖️ Cancel", callback_data="ic:cancel")],
                    ]
                ),
            )
            return
        await query.edit_message_text("Pick a company:", reply_markup=_pick_company_keyboard(companies, page))
        return

    if data.startswith("ic:pickc:"):
        preview = context.user_data.get("pending_import")
        if preview is None:
            await query.edit_message_text("Nothing pending.")
            return
        company_id = int(data.split(":")[2])
        payload, error = await _commit_preview_with_company_id(preview, company_id)
        if error:
            await query.edit_message_text(error)
            return
        context.user_data.pop("pending_import", None)
        company_name, added, updated = payload
        await query.edit_message_text(
            f'Filed under "{company_name}": added {added} file(s), refreshed {updated} already catalogued.'
        )
        return

    if data == "ic:cancel":
        context.user_data.pop("pending_import", None)
        context.user_data.pop("awaiting_new_company_name", None)
        await query.edit_message_text("Discarded.")
        return

    if data == "sm" or data.startswith("sr:"):
        page = int(data.split(":")[1]) if data.startswith("sr:") else 1
        text, keyboard = await _search_home_view(page)
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if data == "sn":
        context.user_data["awaiting_search_text"] = True
        await query.edit_message_text("Send the text you want to search filenames for.")
        return

    if data.startswith("sc:"):
        page = int(data.split(":")[1])
        companies = await _fetch_companies()
        if not companies:
            await query.edit_message_text(
                "No companies yet. Add one via /addlink and pick a company.", reply_markup=_back_to_search_keyboard()
            )
            return
        await query.edit_message_text("Companies:", reply_markup=_companies_keyboard(companies, page))
        return

    if data.startswith("sf:"):
        _, company_id_str, page_str = data.split(":")
        company_id, page = int(company_id_str), int(page_str)
        rows, total = await _fetch_files_by_company(company_id, page)
        if not rows:
            companies = await _fetch_companies()
            await query.edit_message_text("No files under that company.", reply_markup=_companies_keyboard(companies, 1))
            return
        keyboard = _paged_files_keyboard(rows, page, total, page_prefix=f"sf:{company_id}:", back_callback="sc:1")
        await query.edit_message_text(f"Files ({total}):", reply_markup=keyboard)
        return

    if data.startswith("sd:"):
        file_id = int(data.split(":")[1])
        await _render_detail(query, file_id)
        return

    if data.startswith("sg:"):
        file_id = int(data.split(":")[1])
        await _deliver_file(query.message, file_id)
        return

    if data.startswith("sx:"):
        file_id = int(data.split(":")[1])
        row = await _fetch_file(file_id)
        if row is None:
            await query.edit_message_text("That file is already gone.", reply_markup=_back_to_search_keyboard())
            return
        await query.edit_message_text(
            f'Delete "{row.name}" from the catalog? The Drive file itself will not be touched.',
            reply_markup=_delete_confirm_keyboard(file_id),
        )
        return

    if data.startswith("sxy:"):
        file_id = int(data.split(":")[1])
        row = await _do_delete(file_id)
        if row is None:
            await query.edit_message_text("Already gone.", reply_markup=_back_to_search_keyboard())
        else:
            await query.edit_message_text(f'Deleted "{row.name}" from the catalog.', reply_markup=_back_to_search_keyboard())
        return

    if data.startswith("sxn:"):
        file_id = int(data.split(":")[1])
        await _render_detail(query, file_id)
        return

    if data.startswith("sp:"):
        file_id = int(data.split(":")[1])
        row = await _fetch_file(file_id)
        if row is None:
            await query.edit_message_text("That file is gone.", reply_markup=_back_to_search_keyboard())
            return
        context.user_data["pending_replace_file_id"] = file_id
        await query.edit_message_text(f'Send the new Google Drive link to replace "{row.name}" with.')
        return


@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    pending_replace_file_id = context.user_data.pop("pending_replace_file_id", None)
    if pending_replace_file_id is not None:
        row, error = await _do_replace(pending_replace_file_id, text)
        if error:
            await update.message.reply_text(error)
            return
        await update.message.reply_text(
            f'Replaced. "{row.name}" now points at the new Drive file.', reply_markup=_main_menu_keyboard()
        )
        return

    if context.user_data.pop("awaiting_addlink_url", None):
        await _preview_and_reply(update.message, context, text)
        return

    if context.user_data.pop("awaiting_get_query", None):
        try:
            row = await _lookup_file(text)
        except Exception:
            logger.exception("Failed to look up %s", text)
            await update.message.reply_text(
                "Something went wrong looking that up. Try again.", reply_markup=_main_menu_keyboard()
            )
            return
        if row is None:
            await update.message.reply_text(f"No file matching {text!r}.", reply_markup=_main_menu_keyboard())
            return
        await _deliver_file(update.message, row.id)
        return

    if context.user_data.pop("awaiting_new_company_name", None):
        preview: LinkPreview | None = context.user_data.get("pending_import")
        if preview is None:
            await update.message.reply_text("Nothing pending.", reply_markup=_main_menu_keyboard())
            return
        payload, error = await _commit_preview_with_company_name(preview, text)
        if error:
            await update.message.reply_text(error, reply_markup=_main_menu_keyboard())
            return
        context.user_data.pop("pending_import", None)
        company_name, added, updated = payload
        await update.message.reply_text(
            f'Filed under "{company_name}": added {added} file(s), refreshed {updated} already catalogued.',
            reply_markup=_main_menu_keyboard(),
        )
        return

    if context.user_data.pop("awaiting_search_text", None):
        rows = await _fetch_search(text)
        if not rows:
            await update.message.reply_text("No matches.", reply_markup=_main_menu_keyboard())
            return
        keyboard = InlineKeyboardMarkup(
            [[_file_button(row)] for row in rows] + [[InlineKeyboardButton("« Search menu", callback_data="sm")]]
        )
        await update.message.reply_text(f"Found {len(rows)} match(es):", reply_markup=keyboard)
        return


BOT_COMMANDS = [
    BotCommand("start", "Show the welcome menu"),
    BotCommand("addlink", "Add a Google Drive link"),
    BotCommand("cancel", "Cancel current preview"),
    BotCommand("companies", "View companies"),
    BotCommand("search", "Browse or search your files"),
    BotCommand("menu", "Show the button menu"),
    BotCommand("find", "Find a file"),
    BotCommand("get", "Get a file"),
    BotCommand("delete", "Remove from catalog (Drive untouched)"),
    BotCommand("replace", "Replace a file"),
]


async def _post_init(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)


def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addlink", addlink))
    application.add_handler(CommandHandler("cancel", cancel_cmd))
    application.add_handler(CommandHandler("companies", companies_cmd))
    application.add_handler(CommandHandler("search", search_cmd))
    application.add_handler(CommandHandler("menu", menu_cmd))
    application.add_handler(CommandHandler("find", find_cmd))
    application.add_handler(CommandHandler("get", get_cmd))
    application.add_handler(CommandHandler("delete", delete_cmd))
    application.add_handler(CommandHandler("replace", replace_cmd))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return application
