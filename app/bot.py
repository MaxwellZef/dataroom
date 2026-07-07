from __future__ import annotations

import asyncio
import functools
import io
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
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
    stats,
)
from app.config import ALLOWED_TELEGRAM_USER_IDS, TELEGRAM_MAX_FILE_BYTES, TELEGRAM_BOT_TOKEN
from app.db import SessionLocal
from app.drive import DriveClient, DriveFile, DriveLinkError, extract_drive_id
from app.models import File

logger = logging.getLogger(__name__)

MAX_PREVIEW_LINES = 40
TELEGRAM_MESSAGE_LIMIT = 3800
MENU_PAGE_SIZE = 8


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


async def _send_chunked(update: Update, text: str) -> None:
    for start in range(0, len(text), TELEGRAM_MESSAGE_LIMIT):
        await update.message.reply_text(text[start : start + TELEGRAM_MESSAGE_LIMIT])


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


# --- small DB-thread helpers shared by commands and the inline menu ---


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
    finally:
        session.close()


# --- inline keyboards for the /search menu ---


def _file_button(row: File) -> InlineKeyboardButton:
    return InlineKeyboardButton(_truncate(row.name), callback_data=f"sd:{row.id}")


def _root_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏢 By company", callback_data="sc:1")],
            [InlineKeyboardButton("🔤 By filename", callback_data="sn")],
            [InlineKeyboardButton("🕒 Recent", callback_data="sr:1")],
        ]
    )


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
        await query.edit_message_text("That file is gone.", reply_markup=_root_menu_keyboard())
        return
    await query.edit_message_text(_file_detail_text(row), reply_markup=_detail_keyboard(file_id))


# --- commands ---


@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Dataroom bot.\n\n"
        "/addlink <drive url> - preview a Google Drive file or folder\n"
        "/confirm [number | new <name>] - file the last preview under a company\n"
        "/cancel - discard the last preview\n"
        "/companies - list companies and their file counts\n"
        "/search - browse/search with buttons (company, filename, recent)\n"
        "/list [page] - list catalogued files\n"
        "/find <text> - search by filename\n"
        "/get <id or name> - fetch a file\n"
        "/delete <id> - remove a file from the catalog (Drive is untouched)\n"
        "/replace <id> <new drive url> - point a catalog entry at a new Drive file\n"
        "/stats - catalog totals"
    )


@owner_only
async def addlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addlink <google drive url>")
        return

    url = context.args[0]
    await update.message.reply_text("Reading that link from Drive...")

    def _run():
        preview = preview_link(url, DriveClient())
        session = SessionLocal()
        try:
            return preview, list_companies(session)
        finally:
            session.close()

    try:
        preview, companies = await asyncio.to_thread(_run)
    except DriveLinkError as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Failed to preview link %s", url)
        await update.message.reply_text(
            "Couldn't read that link. Make sure it's shared as "
            "\"Anyone with the link\" and try again."
        )
        return

    if not preview.files:
        await update.message.reply_text("That link resolved but no files were found in it.")
        return

    context.user_data["pending_import"] = preview

    lines = [f"Found {len(preview.files)} file(s) in \"{preview.root_name}\":"]
    lines += [f"- {f.name}" for f in preview.files[:MAX_PREVIEW_LINES]]
    if len(preview.files) > MAX_PREVIEW_LINES:
        lines.append(f"...and {len(preview.files) - MAX_PREVIEW_LINES} more")
    lines.append("")

    if preview.suggested_company:
        lines.append(f'Suggested company (from the folder name): "{preview.suggested_company}"')
        lines.append("/confirm - file everything under it")
        lines.append("/confirm <number> - use an existing company below instead")
        lines.append("/confirm new <name> - use a different new company name")
    else:
        lines.append("This is a single file, so pick a company:")
        lines.append("/confirm <number> - use an existing company below")
        lines.append("/confirm new <name> - create a new company")

    if companies:
        lines.append("")
        lines.append("Existing companies:")
        lines += [f"{i}. {company.name} ({count} files)" for i, (company, count) in enumerate(companies, start=1)]

    lines.append("")
    lines.append("/cancel - discard this")

    await _send_chunked(update, "\n".join(lines))


@owner_only
async def confirm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    preview: LinkPreview | None = context.user_data.get("pending_import")
    if preview is None:
        await update.message.reply_text("Nothing pending. Use /addlink <url> first.")
        return

    def _run():
        session = SessionLocal()
        try:
            companies = list_companies(session)
            if not context.args:
                if not preview.suggested_company:
                    return None, (
                        "No company specified. Use /confirm <number> or /confirm new <name>."
                    )
                company = get_or_create_company(session, preview.suggested_company)
            elif context.args[0].lower() == "new":
                name = " ".join(context.args[1:]).strip()
                if not name:
                    return None, "Usage: /confirm new <company name>"
                company = get_or_create_company(session, name)
            elif context.args[0].isdigit():
                idx = int(context.args[0])
                if not (1 <= idx <= len(companies)):
                    return None, f"No company numbered {idx}. Check /companies."
                company = companies[idx - 1][0]
            else:
                return None, "Usage: /confirm | /confirm <number> | /confirm new <name>"

            result = commit_import(session, preview, company)
            return (company.name, result.added, result.updated), None
        finally:
            session.close()

    payload, error = await asyncio.to_thread(_run)
    if error:
        await update.message.reply_text(error)
        return

    company_name, added, updated = payload
    context.user_data.pop("pending_import", None)
    await update.message.reply_text(
        f'Filed under "{company_name}": added {added} file(s), refreshed {updated} already catalogued.'
    )


@owner_only
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.pop("pending_import", None) is None:
        await update.message.reply_text("Nothing pending.")
    else:
        await update.message.reply_text("Discarded.")


@owner_only
async def companies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    companies = await _fetch_companies()
    if not companies:
        await update.message.reply_text("No companies yet. They're created via /addlink then /confirm.")
        return

    lines = [f"{i}. {company.name} ({count} files)" for i, (company, count) in enumerate(companies, start=1)]
    await _send_chunked(update, "\n".join(lines))


def _format_file_line(row: File) -> str:
    line = f"[{row.id}] {row.name} ({_human_size(row.size_bytes)})"
    if row.company:
        line += f" — {row.company.name}"
    return line


@owner_only
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = 1
    if context.args and context.args[0].isdigit():
        page = int(context.args[0])

    def _run():
        session = SessionLocal()
        try:
            return list_files(session, page=page, page_size=20)
        finally:
            session.close()

    rows, total = await asyncio.to_thread(_run)
    if not rows:
        await update.message.reply_text("No files yet. Add one with /addlink <url>.")
        return

    lines = [_format_file_line(row) for row in rows]
    header = f"Page {page} - {total} file(s) total\n"
    await _send_chunked(update, header + "\n".join(lines))


@owner_only
async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /find <text to search for>")
        return

    rows = await _fetch_search(" ".join(context.args))
    if not rows:
        await update.message.reply_text("No matches.")
        return

    lines = [_format_file_line(row) for row in rows]
    await _send_chunked(update, "\n".join(lines))


@owner_only
async def get_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /get <catalog id or filename>")
        return

    identifier = " ".join(context.args)

    def _run():
        session = SessionLocal()
        try:
            return find_file(session, identifier)
        finally:
            session.close()

    try:
        row = await asyncio.to_thread(_run)
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
        await update.message.reply_text("Usage: /delete <catalog id> (see /list or /find for ids)")
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
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def _run():
        session = SessionLocal()
        try:
            return stats(session)
        finally:
            session.close()

    count, total_size = await asyncio.to_thread(_run)
    await update.message.reply_text(f"{count} file(s) catalogued, {_human_size(total_size)} total.")


@owner_only
async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Search the dataroom:", reply_markup=_root_menu_keyboard())


# --- inline menu callback + free-text follow-ups (search-by-name, replace link) ---


@owner_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "sm":
        await query.edit_message_text("Search the dataroom:", reply_markup=_root_menu_keyboard())
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
                "No companies yet. Add one via /addlink then /confirm.", reply_markup=_root_menu_keyboard()
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

    if data.startswith("sr:"):
        page = int(data.split(":")[1])
        rows, total = await _fetch_recent(page)
        if not rows:
            await query.edit_message_text("No files yet.", reply_markup=_root_menu_keyboard())
            return
        keyboard = _paged_files_keyboard(rows, page, total, page_prefix="sr:", back_callback="sm")
        await query.edit_message_text(f"Recent ({total}):", reply_markup=keyboard)
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
            await query.edit_message_text("That file is already gone.", reply_markup=_root_menu_keyboard())
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
            await query.edit_message_text("Already gone.", reply_markup=_root_menu_keyboard())
        else:
            await query.edit_message_text(f'Deleted "{row.name}" from the catalog.', reply_markup=_root_menu_keyboard())
        return

    if data.startswith("sxn:"):
        file_id = int(data.split(":")[1])
        await _render_detail(query, file_id)
        return

    if data.startswith("sp:"):
        file_id = int(data.split(":")[1])
        row = await _fetch_file(file_id)
        if row is None:
            await query.edit_message_text("That file is gone.", reply_markup=_root_menu_keyboard())
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
        await update.message.reply_text(f'Replaced. "{row.name}" now points at the new Drive file.')
        return

    if context.user_data.pop("awaiting_search_text", None):
        rows = await _fetch_search(text)
        if not rows:
            await update.message.reply_text("No matches.", reply_markup=_root_menu_keyboard())
            return
        keyboard = InlineKeyboardMarkup(
            [[_file_button(row)] for row in rows] + [[InlineKeyboardButton("« Menu", callback_data="sm")]]
        )
        await update.message.reply_text(f"Found {len(rows)} match(es):", reply_markup=keyboard)
        return


def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addlink", addlink))
    application.add_handler(CommandHandler("confirm", confirm_cmd))
    application.add_handler(CommandHandler("cancel", cancel_cmd))
    application.add_handler(CommandHandler("companies", companies_cmd))
    application.add_handler(CommandHandler("search", search_cmd))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("find", find_cmd))
    application.add_handler(CommandHandler("get", get_cmd))
    application.add_handler(CommandHandler("delete", delete_cmd))
    application.add_handler(CommandHandler("replace", replace_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return application
