import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Source(Base):
    """A Drive link the user added (a single file or a whole folder)."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(2048))
    drive_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))  # "file" | "folder"
    added_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    files: Mapped[list["File"]] = relationship(back_populates="source")


class Company(Base):
    """A folder of documents you've filed under one name, e.g. a company."""

    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("name", name="uq_companies_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    added_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    files: Mapped[list["File"]] = relationship(back_populates="company")


class File(Base):
    """One catalog entry pointing at a file that actually lives in Drive."""

    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("drive_file_id", name="uq_files_drive_file_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(1024))
    drive_file_id: Mapped[str] = mapped_column(String(128), index=True)
    mime_type: Mapped[str] = mapped_column(String(256))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    web_view_link: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    telegram_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    added_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    source: Mapped[Source | None] = relationship(back_populates="files")

    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    company: Mapped[Company | None] = relationship(back_populates="files")
