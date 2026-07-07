"""Google Drive access using a plain API key (no OAuth).

This only works for files/folders shared as "Anyone with the link" (or
public). That's the common case for links people hand around, and it keeps
setup to "create an API key" instead of a full OAuth consent flow. If a
link 403s, the fix is to change that file's sharing setting in Drive, not
a config change here.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from app.config import GOOGLE_API_KEY

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

# Native Google Docs types can't be downloaded raw; they must be exported
# to a real file format.
EXPORT_MIME_MAP = {
    "application/vnd.google-apps.document": (
        "application/pdf",
        ".pdf",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": (
        "image/png",
        ".png",
    ),
}

_ID_PATTERNS = [
    re.compile(r"/d/([a-zA-Z0-9_-]{10,})"),
    re.compile(r"/folders/([a-zA-Z0-9_-]{10,})"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]{10,})"),
]
_BARE_ID = re.compile(r"^[a-zA-Z0-9_-]{10,}$")


class DriveLinkError(ValueError):
    pass


def extract_drive_id(url_or_id: str) -> str:
    """Pull a Drive file/folder ID out of a share link, or accept a bare ID."""

    candidate = url_or_id.strip()
    if _BARE_ID.match(candidate) and "/" not in candidate:
        return candidate

    for pattern in _ID_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group(1)

    raise DriveLinkError(f"Couldn't find a Drive file/folder ID in: {url_or_id!r}")


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int | None
    web_view_link: str | None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME_TYPE


class DriveClient:
    _FIELDS = "id,name,mimeType,size,webViewLink,trashed"

    def __init__(self, api_key: str | None = None):
        self._service = build(
            "drive",
            "v3",
            developerKey=api_key or GOOGLE_API_KEY,
            static_discovery=False,
            cache_discovery=False,
        )

    def get_metadata(self, file_id: str) -> DriveFile:
        data = self._service.files().get(
            fileId=file_id,
            fields=self._FIELDS,
            supportsAllDrives=True,
        ).execute()
        return DriveFile(
            id=data["id"],
            name=data["name"],
            mime_type=data["mimeType"],
            size=int(data["size"]) if data.get("size") else None,
            web_view_link=data.get("webViewLink"),
        )

    def list_folder(self, folder_id: str, recursive: bool = True) -> list[DriveFile]:
        """Flatten a folder (and, by default, its subfolders) into files."""

        results: list[DriveFile] = []
        page_token = None
        while True:
            response = self._service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields=f"nextPageToken, files({self._FIELDS})",
                pageSize=200,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()

            for item in response.get("files", []):
                drive_file = DriveFile(
                    id=item["id"],
                    name=item["name"],
                    mime_type=item["mimeType"],
                    size=int(item["size"]) if item.get("size") else None,
                    web_view_link=item.get("webViewLink"),
                )
                if drive_file.is_folder:
                    if recursive:
                        results.extend(self.list_folder(drive_file.id, recursive=True))
                else:
                    results.append(drive_file)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return results

    def download_bytes(self, drive_file: DriveFile) -> tuple[bytes, str]:
        """Return (content, filename) ready to hand to a chat client."""

        export = EXPORT_MIME_MAP.get(drive_file.mime_type)
        if export:
            export_mime, extension = export
            request = self._service.files().export(fileId=drive_file.id, mimeType=export_mime)
            filename = drive_file.name if drive_file.name.endswith(extension) else drive_file.name + extension
        else:
            request = self._service.files().get_media(fileId=drive_file.id)
            filename = drive_file.name

        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        return buffer.getvalue(), filename
