from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.catalog import (
    commit_import,
    delete_file,
    files_by_company,
    find_file,
    get_or_create_company,
    list_companies,
    list_files,
    preview_link,
    replace_file_source,
    search_files,
    stats,
)
from app.db import Base
from app.drive import DriveFile


class FakeDriveClient:
    """Stands in for DriveClient without hitting the network."""

    def __init__(self, root: DriveFile, children: list[DriveFile] | None = None):
        self._root = root
        self._children = children or []

    def get_metadata(self, file_id):
        assert file_id == self._root.id
        return self._root

    def list_folder(self, folder_id, recursive=True):
        assert folder_id == self._root.id
        return self._children


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    # expire_on_commit=False matches app.db.SessionLocal: without it, accessing
    # attributes on a row returned by delete_file() after commit would raise
    # ObjectDeletedError since SQLAlchemy would try to refresh it from the DB.
    return Session(bind=engine, expire_on_commit=False)


def test_preview_single_file_has_no_suggested_company():
    root = DriveFile(
        id="file1aaaaaaaaaaaaa",
        name="report.pdf",
        mime_type="application/pdf",
        size=1024,
        web_view_link="https://drive.google.com/file/d/file1aaaaaaaaaaaaa/view",
    )
    preview = preview_link(
        "https://drive.google.com/file/d/file1aaaaaaaaaaaaa/view", FakeDriveClient(root)
    )

    assert preview.kind == "file"
    assert preview.suggested_company is None
    assert [f.name for f in preview.files] == ["report.pdf"]


def test_preview_folder_suggests_company_and_expands_children():
    root = DriveFile(
        id="folder1aaaaaaaaaaaaa",
        name="Acme Corp",
        mime_type="application/vnd.google-apps.folder",
        size=None,
        web_view_link=None,
    )
    children = [
        DriveFile(id="f1aaaaaaaaaaaaa", name="a.pdf", mime_type="application/pdf", size=10, web_view_link=None),
        DriveFile(id="f2aaaaaaaaaaaaa", name="b.docx", mime_type="application/msword", size=20, web_view_link=None),
    ]
    preview = preview_link(
        "https://drive.google.com/drive/folders/folder1aaaaaaaaaaaaa",
        FakeDriveClient(root, children),
    )

    assert preview.kind == "folder"
    assert preview.suggested_company == "Acme Corp"
    assert {f.name for f in preview.files} == {"a.pdf", "b.docx"}


def test_commit_import_files_under_company():
    session = make_session()
    root = DriveFile(
        id="folder1aaaaaaaaaaaaa",
        name="Acme Corp",
        mime_type="application/vnd.google-apps.folder",
        size=None,
        web_view_link=None,
    )
    children = [
        DriveFile(id="f1aaaaaaaaaaaaa", name="a.pdf", mime_type="application/pdf", size=10, web_view_link=None),
        DriveFile(id="f2aaaaaaaaaaaaa", name="b.docx", mime_type="application/msword", size=20, web_view_link=None),
    ]
    preview = preview_link(
        "https://drive.google.com/drive/folders/folder1aaaaaaaaaaaaa",
        FakeDriveClient(root, children),
    )

    company = get_or_create_company(session, preview.suggested_company)
    result = commit_import(session, preview, company)

    assert result.added == 2
    assert result.updated == 0

    rows, total = list_files(session)
    assert total == 2
    assert all(row.company.name == "Acme Corp" for row in rows)

    companies = list_companies(session)
    assert companies == [(company, 2)]


def test_commit_import_is_idempotent_and_can_move_company():
    session = make_session()
    root = DriveFile(
        id="file1aaaaaaaaaaaaa", name="report.pdf", mime_type="application/pdf", size=1024, web_view_link=None
    )
    client = FakeDriveClient(root)
    preview = preview_link("https://drive.google.com/file/d/file1aaaaaaaaaaaaa/view", client)

    company_a = get_or_create_company(session, "Company A")
    commit_import(session, preview, company_a)

    company_b = get_or_create_company(session, "Company B")
    result = commit_import(session, preview, company_b)

    assert result.added == 0
    assert result.updated == 1
    _, total = list_files(session)
    assert total == 1
    rows, _ = list_files(session)
    assert rows[0].company.name == "Company B"


def test_get_or_create_company_is_case_insensitive():
    session = make_session()
    first = get_or_create_company(session, "Acme Corp")
    second = get_or_create_company(session, "acme corp")
    assert first.id == second.id


def test_search_and_find():
    session = make_session()
    root = DriveFile(
        id="folder1aaaaaaaaaaaaa",
        name="Docs",
        mime_type="application/vnd.google-apps.folder",
        size=None,
        web_view_link=None,
    )
    children = [
        DriveFile(
            id="f1aaaaaaaaaaaaa", name="Invoice-2024.pdf", mime_type="application/pdf", size=10, web_view_link=None
        ),
        DriveFile(
            id="f2aaaaaaaaaaaaa", name="Contract.docx", mime_type="application/msword", size=20, web_view_link=None
        ),
    ]
    preview = preview_link(
        "https://drive.google.com/drive/folders/folder1aaaaaaaaaaaaa",
        FakeDriveClient(root, children),
    )
    company = get_or_create_company(session, "Docs")
    commit_import(session, preview, company)

    assert [f.name for f in search_files(session, "invoice")] == ["Invoice-2024.pdf"]

    by_id = find_file(session, "1")
    assert by_id is not None and by_id.name in {"Invoice-2024.pdf", "Contract.docx"}

    by_name = find_file(session, "contract.docx")
    assert by_name is not None
    assert by_name.name == "Contract.docx"

    assert find_file(session, "nope-not-here") is None


def test_stats():
    session = make_session()
    root = DriveFile(
        id="file1aaaaaaaaaaaaa", name="report.pdf", mime_type="application/pdf", size=1000, web_view_link=None
    )
    preview = preview_link("https://drive.google.com/file/d/file1aaaaaaaaaaaaa/view", FakeDriveClient(root))
    company = get_or_create_company(session, "Some Company")
    commit_import(session, preview, company)

    count, total_size = stats(session)
    assert count == 1
    assert total_size == 1000


def test_files_by_company_paginates():
    session = make_session()
    root = DriveFile(
        id="folder1aaaaaaaaaaaaa",
        name="Acme Corp",
        mime_type="application/vnd.google-apps.folder",
        size=None,
        web_view_link=None,
    )
    children = [
        DriveFile(id=f"f{i}aaaaaaaaaaaaa", name=f"doc{i}.pdf", mime_type="application/pdf", size=1, web_view_link=None)
        for i in range(3)
    ]
    preview = preview_link(
        "https://drive.google.com/drive/folders/folder1aaaaaaaaaaaaa",
        FakeDriveClient(root, children),
    )
    company = get_or_create_company(session, "Acme Corp")
    commit_import(session, preview, company)

    page1, total = files_by_company(session, company.id, page=1, page_size=2)
    assert total == 3
    assert len(page1) == 2

    page2, _ = files_by_company(session, company.id, page=2, page_size=2)
    assert len(page2) == 1


def test_delete_file_removes_only_catalog_entry():
    session = make_session()
    root = DriveFile(
        id="file1aaaaaaaaaaaaa", name="report.pdf", mime_type="application/pdf", size=1024, web_view_link=None
    )
    preview = preview_link("https://drive.google.com/file/d/file1aaaaaaaaaaaaa/view", FakeDriveClient(root))
    company = get_or_create_company(session, "Some Company")
    result = commit_import(session, preview, company)
    file_id = result.files[0].id

    deleted = delete_file(session, file_id)
    assert deleted is not None
    assert deleted.name == "report.pdf"

    _, total = list_files(session)
    assert total == 0
    assert delete_file(session, file_id) is None


def test_replace_file_source_swaps_drive_target():
    session = make_session()
    root = DriveFile(
        id="file1aaaaaaaaaaaaa", name="old.pdf", mime_type="application/pdf", size=100, web_view_link=None
    )
    preview = preview_link("https://drive.google.com/file/d/file1aaaaaaaaaaaaa/view", FakeDriveClient(root))
    company = get_or_create_company(session, "Some Company")
    result = commit_import(session, preview, company)
    file_id = result.files[0].id

    new_drive_file = DriveFile(
        id="file2bbbbbbbbbbbbb", name="new.pdf", mime_type="application/pdf", size=200, web_view_link=None
    )
    updated, error = replace_file_source(session, file_id, new_drive_file)

    assert error is None
    assert updated.id == file_id
    assert updated.name == "new.pdf"
    assert updated.drive_file_id == "file2bbbbbbbbbbbbb"
    assert updated.company.name == "Some Company"


def test_replace_file_source_rejects_conflicting_drive_id():
    session = make_session()
    company = get_or_create_company(session, "Some Company")

    preview_a = preview_link(
        "https://drive.google.com/file/d/file1aaaaaaaaaaaaa/view",
        FakeDriveClient(
            DriveFile(id="file1aaaaaaaaaaaaa", name="a.pdf", mime_type="application/pdf", size=1, web_view_link=None)
        ),
    )
    result_a = commit_import(session, preview_a, company)

    preview_b = preview_link(
        "https://drive.google.com/file/d/file2bbbbbbbbbbbbb/view",
        FakeDriveClient(
            DriveFile(id="file2bbbbbbbbbbbbb", name="b.pdf", mime_type="application/pdf", size=1, web_view_link=None)
        ),
    )
    commit_import(session, preview_b, company)

    conflicting_target = DriveFile(
        id="file2bbbbbbbbbbbbb", name="b.pdf", mime_type="application/pdf", size=1, web_view_link=None
    )
    updated, error = replace_file_source(session, result_a.files[0].id, conflicting_target)

    assert updated is None
    assert error is not None
    assert "already catalogued" in error
