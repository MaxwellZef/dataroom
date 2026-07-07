import pytest

from app.drive import DriveLinkError, extract_drive_id


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing", "1AbCdEfGhIjKlMnOp"),
        ("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp", "1AbCdEfGhIjKlMnOp"),
        ("https://drive.google.com/uc?id=1AbCdEfGhIjKlMnOp&export=download", "1AbCdEfGhIjKlMnOp"),
        ("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp", "1AbCdEfGhIjKlMnOp"),
        ("https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjKlMnOp?usp=sharing", "1AbCdEfGhIjKlMnOp"),
        ("https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit", "1AbCdEfGhIjKlMnOp"),
        ("1AbCdEfGhIjKlMnOp", "1AbCdEfGhIjKlMnOp"),
    ],
)
def test_extract_drive_id(url, expected):
    assert extract_drive_id(url) == expected


def test_extract_drive_id_rejects_garbage():
    with pytest.raises(DriveLinkError):
        extract_drive_id("https://example.com/not-a-drive-link")
