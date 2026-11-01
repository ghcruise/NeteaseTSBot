from __future__ import annotations

from collections.abc import Generator
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


def get_database_url() -> str:
    return os.getenv("DATABASE_URL") or os.getenv("TSBOT_DATABASE_URL") or "sqlite:///./tsbot.db"


def get_sqlite_db_path() -> str | None:
    database_url = get_database_url()
    sqlite_prefix = "sqlite:///"
    if not database_url.startswith(sqlite_prefix):
        return None
    return database_url.removeprefix(sqlite_prefix)


_engine = create_engine(get_database_url(), connect_args={"check_same_thread": False})
_SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def create_db_and_tables() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(_engine)
    inspector = inspect(_engine)
    table_names = inspector.get_table_names()
    if "queue_items" not in table_names:
        return

    columns = {column["name"] for column in inspector.get_columns("queue_items")}
    with _engine.begin() as connection:
        if "position" not in columns:
            connection.execute(text("ALTER TABLE queue_items ADD COLUMN position INTEGER NOT NULL DEFAULT 0"))
        if "source_page" not in columns:
            connection.execute(text("ALTER TABLE queue_items ADD COLUMN source_page INTEGER"))
        if "source_cid" not in columns:
            connection.execute(text("ALTER TABLE queue_items ADD COLUMN source_cid INTEGER"))
        connection.execute(text("UPDATE queue_items SET position = id WHERE position = 0"))

    if "history_items" in table_names:
        history_columns = {column["name"] for column in inspector.get_columns("history_items")}
        with _engine.begin() as connection:
            if "source_page" not in history_columns:
                connection.execute(text("ALTER TABLE history_items ADD COLUMN source_page INTEGER"))
            if "source_cid" not in history_columns:
                connection.execute(text("ALTER TABLE history_items ADD COLUMN source_cid INTEGER"))


def new_session() -> Session:
    return _SessionLocal()


def get_session() -> Generator[Session, None, None]:
    session = _SessionLocal()
    try:
        yield session
    finally:
        session.close()
