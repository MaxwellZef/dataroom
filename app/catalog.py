from __future__ import annotations

import difflib
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.drive import DriveClient, DriveFile, extract_drive_id
from app.models import Company, File, Source


@dataclass
class LinkPreview:
    """What a Drive link resolves to, before anything is written to the catalog."""

    url: str
    drive_id: str
    kind: str  # "file" | "folder"
    root_name: str
    files: list[DriveFile]
    suggested_company: str | None


@dataclass
class ImportResult:
    added: int
    updated: int
    files: list[File]


def preview_link(url: str, drive: DriveClient | None = None) -> LinkPreview:
    """Read a Drive share link and report what's in it, without touching the DB."""

    drive = drive or DriveClient()
    drive_id = extract_drive_id(url)
    root = drive.get_metadata(drive_id)

    if root.is_folder:
        return LinkPreview(
            url=url,
            drive_id=drive_id,
            kind="folder",
            root_name=root.name,
            files=drive.list_folder(drive_id, recursive=True),
            suggested_company=root.name,
        )

    return LinkPreview(
        url=url,
        drive_id=drive_id,
        kind="file",
        root_name=root.name,
        files=[root],
        suggested_company=None,
    )


def _upsert_file(
    session: Session, drive_file: DriveFile, source: Source | None, company: Company | None
) -> tuple[File, bool]:
    """Insert or refresh a catalog row for one Drive file. Returns (row, is_new)."""

    existing = session.query(File).filter_by(drive_file_id=drive_file.id).one_or_none()
    if existing:
        existing.name = drive_file.name
        existing.mime_type = drive_file.mime_type
        existing.size_bytes = drive_file.size
        existing.web_view_link = drive_file.web_view_link
        existing.company = company
        return existing, False

    row = File(
        name=drive_file.name,
        drive_file_id=drive_file.id,
        mime_type=drive_file.mime_type,
        size_bytes=drive_file.size,
        web_view_link=drive_file.web_view_link,
        source=source,
        company=company,
    )
    session.add(row)
    return row, True


def commit_import(session: Session, preview: LinkPreview, company: Company) -> ImportResult:
    """Write a previewed link's files into the catalog under the given company."""

    source = Source(url=preview.url, drive_id=preview.drive_id, kind=preview.kind)
    session.add(source)
    session.flush()

    added = 0
    updated = 0
    rows: list[File] = []
    for drive_file in preview.files:
        row, is_new = _upsert_file(session, drive_file, source, company)
        rows.append(row)
        if is_new:
            added += 1
        else:
            updated += 1

    session.commit()
    return ImportResult(added=added, updated=updated, files=rows)


def get_or_create_company(session: Session, name: str) -> Company:
    name = name.strip()
    existing = session.query(Company).filter(func.lower(Company.name) == name.lower()).one_or_none()
    if existing:
        return existing

    company = Company(name=name)
    session.add(company)
    session.flush()
    return company


def list_companies(session: Session) -> list[tuple[Company, int]]:
    """Every company with how many files are filed under it, alphabetical."""

    return (
        session.query(Company, func.count(File.id))
        .outerjoin(File, File.company_id == Company.id)
        .group_by(Company.id)
        .order_by(Company.name)
        .all()
    )


def delete_company(session: Session, company_id: int) -> tuple[str, int] | None:
    """Delete a company. Its files are never deleted — they just become unfiled."""

    company = session.query(Company).filter_by(id=company_id).one_or_none()
    if company is None:
        return None

    name = company.name
    files = session.query(File).filter_by(company_id=company_id).all()
    for file_row in files:
        file_row.company_id = None
    affected = len(files)
    session.delete(company)
    session.commit()
    return name, affected


def list_files(session: Session, page: int = 1, page_size: int = 20) -> tuple[list[File], int]:
    total = session.query(func.count(File.id)).scalar() or 0
    rows = (
        session.query(File)
        .options(joinedload(File.company))
        .order_by(File.added_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return rows, total


def files_by_company(
    session: Session, company_id: int, page: int = 1, page_size: int = 8
) -> tuple[list[File], int]:
    query = session.query(File).options(joinedload(File.company)).filter(File.company_id == company_id)
    total = query.with_entities(func.count(File.id)).scalar() or 0
    rows = (
        query.order_by(File.name)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return rows, total


def get_file(session: Session, file_id: int) -> File | None:
    return (
        session.query(File)
        .options(joinedload(File.company))
        .filter_by(id=file_id)
        .one_or_none()
    )


def search_files(session: Session, query: str, limit: int = 20) -> list[File]:
    like = f"%{query}%"
    return (
        session.query(File)
        .options(joinedload(File.company))
        .filter(File.name.ilike(like))
        .order_by(File.added_at.desc())
        .limit(limit)
        .all()
    )


def resolve_file_query(session: Session, identifier: str) -> tuple[File | None, list[File], bool]:
    """Resolve a /get-style query by filename.

    Every file whose name contains the text is a candidate: only an exact
    name match resolves outright — any substring match, even a single one,
    comes back as a list so the caller can show a confirmation instead of
    silently grabbing a file the user didn't explicitly name. E.g.
    searching "KTP" should surface every file with "KTP" in the name for
    the user to pick, not guess at one of them.

    If there's no substring match at all (e.g. a typo — "KTP joni" instead
    of "KTP John"), falls back to typo-tolerant fuzzy matching against every
    filename so a small mistake doesn't come back as a flat "no results".

    Returns (exact_match, candidates, candidates_are_fuzzy_guesses).
    """

    identifier = identifier.strip()
    matches = search_files(session, identifier, limit=20)
    if matches:
        exact = next((m for m in matches if m.name.lower() == identifier.lower()), None)
        if exact:
            return exact, [], False
        return None, matches, False

    return None, _fuzzy_match_files(session, identifier), True


def _fuzzy_match_files(session: Session, query: str, limit: int = 5) -> list[File]:
    """Typo-tolerant fallback: rank every filename by similarity to the query."""

    all_files = session.query(File).options(joinedload(File.company)).all()
    if not all_files:
        return []

    by_lower_name: dict[str, File] = {}
    for file_row in all_files:
        by_lower_name.setdefault(file_row.name.lower(), file_row)

    close_names = difflib.get_close_matches(query.lower(), by_lower_name.keys(), n=limit, cutoff=0.6)
    return [by_lower_name[name] for name in close_names]


def record_telegram_file_id(session: Session, file_row: File, telegram_file_id: str) -> None:
    file_row.telegram_file_id = telegram_file_id
    session.add(file_row)
    session.commit()


def delete_file(session: Session, file_id: int) -> File | None:
    """Remove a catalog entry. The underlying Drive file is never touched."""

    row = session.query(File).filter_by(id=file_id).one_or_none()
    if row is None:
        return None
    session.delete(row)
    session.commit()
    return row


def rename_file(session: Session, file_id: int, new_name: str) -> File | None:
    """Rename a catalog entry's display name. The underlying Drive file is never
    renamed, and the custom name only lasts until this file is next synced —
    commit_import() always resets File.name to whatever Drive reports, so
    re-running /addlink on the same file/folder restores the original name.
    """

    row = session.query(File).filter_by(id=file_id).one_or_none()
    if row is None:
        return None
    row.name = new_name.strip()
    session.commit()
    return row


def replace_file_source(
    session: Session, file_id: int, new_drive_file: DriveFile
) -> tuple[File | None, str | None]:
    """Point an existing catalog entry at a different Drive file (e.g. a renewed contract)."""

    row = session.query(File).filter_by(id=file_id).one_or_none()
    if row is None:
        return None, "No such file in the catalog."

    conflict = (
        session.query(File)
        .filter(File.drive_file_id == new_drive_file.id, File.id != file_id)
        .one_or_none()
    )
    if conflict:
        return None, f'That Drive file is already catalogued as "{conflict.name}" (id {conflict.id}).'

    row.drive_file_id = new_drive_file.id
    row.name = new_drive_file.name
    row.mime_type = new_drive_file.mime_type
    row.size_bytes = new_drive_file.size
    row.web_view_link = new_drive_file.web_view_link
    row.telegram_file_id = None
    session.commit()
    return row, None
