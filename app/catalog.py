from __future__ import annotations

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


def find_file(session: Session, identifier: str) -> File | None:
    """Resolve a /get argument: a numeric catalog id, or a name match."""

    identifier = identifier.strip()
    if identifier.isdigit():
        row = session.query(File).filter_by(id=int(identifier)).one_or_none()
        if row:
            return row

    exact = session.query(File).filter(func.lower(File.name) == identifier.lower()).one_or_none()
    if exact:
        return exact

    matches = search_files(session, identifier, limit=1)
    return matches[0] if matches else None


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


def stats(session: Session) -> tuple[int, int]:
    count = session.query(func.count(File.id)).scalar() or 0
    total_size = session.query(func.coalesce(func.sum(File.size_bytes), 0)).scalar() or 0
    return count, total_size
