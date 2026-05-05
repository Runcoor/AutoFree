"""SQLAlchemy 2.0 base — engine + session + declarative base。

支持 SQLite / Postgres / MySQL,具体 URL 走 settings.resolved_database_url。
SQLite 默认开 WAL + NORMAL synchronous,提升并发读 + 减少 fsync。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from autofree.settings import get_settings


def _make_engine() -> Engine:
    settings = get_settings()
    url = settings.resolved_database_url
    is_sqlite = url.startswith("sqlite")
    eng = create_engine(
        url,
        echo=settings.debug,
        future=True,
        pool_pre_ping=not is_sqlite,
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )
    if is_sqlite:
        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return eng


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
