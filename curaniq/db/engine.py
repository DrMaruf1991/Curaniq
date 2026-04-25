"""
SQLAlchemy engine + session factory.

Environment-driven URL, fail-closed in clinician_prod when the configured
database is unreachable. Defaults to SQLite for local development/tests so
the engine can boot without a running Postgres.

Production deployment MUST set CURANIQ_DATABASE_URL to a real Postgres URL.
"""
from __future__ import annotations
import os
import logging
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker, Session

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# URL resolution
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SQLITE_URL = "sqlite:///./curaniq_dev.db"


def db_url() -> str:
    """Return the configured DB URL.

    Resolution order:
      1. CURANIQ_DATABASE_URL  (production)
      2. CURANIQ_DB_URL        (alias)
      3. DEFAULT_SQLITE_URL    (dev/test fallback)
    """
    return (
        os.environ.get("CURANIQ_DATABASE_URL")
        or os.environ.get("CURANIQ_DB_URL")
        or DEFAULT_SQLITE_URL
    )


def is_postgres() -> bool:
    """Whether the active URL targets Postgres."""
    url = db_url().lower()
    return url.startswith("postgresql") or url.startswith("postgres://")


def is_sqlite() -> bool:
    return db_url().lower().startswith("sqlite")


# ─────────────────────────────────────────────────────────────────────────────
# Engine + session — singleton, thread-safe init
# ─────────────────────────────────────────────────────────────────────────────

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None
_init_lock = threading.Lock()


def get_engine() -> Engine:
    """Lazily build the SQLAlchemy engine."""
    global _engine
    if _engine is not None:
        return _engine
    with _init_lock:
        if _engine is not None:
            return _engine
        url = db_url()
        if os.environ.get("CURANIQ_ENV", "demo").lower() == "clinician_prod" and is_sqlite():
            raise RuntimeError("clinician_prod requires PostgreSQL; SQLite fallback is forbidden.")
        if is_sqlite():
            # check_same_thread=False so test threads can share the in-memory DB
            _engine = create_engine(
                url,
                connect_args={"check_same_thread": False},
                pool_pre_ping=True,
                echo=False,
            )
            # SQLite does not enforce foreign keys by default
            @event.listens_for(_engine, "connect")
            def _enable_fk(dbapi_conn, _conn_record):  # noqa: ARG001
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        else:
            _engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=int(os.environ.get("CURANIQ_DB_POOL_SIZE", "5")),
                max_overflow=int(os.environ.get("CURANIQ_DB_POOL_OVERFLOW", "10")),
                pool_recycle=int(os.environ.get("CURANIQ_DB_POOL_RECYCLE", "3600")),
                echo=False,
            )
        # Verify reachability immediately — fail-closed in clinician_prod
        try:
            with _engine.connect() as conn:
                # 1) is the cheapest portable check; works on Postgres + SQLite
                conn.execute(_select_one())
        except OperationalError as e:
            env = os.environ.get("CURANIQ_ENV", "demo").lower()
            if env == "clinician_prod":
                logger.error("clinician_prod: database unreachable at %s", url)
                raise RuntimeError(
                    f"clinician_prod requires reachable database; got: {e}"
                ) from e
            logger.warning("Database unreachable (env=%s, url=%s): %s", env, url, e)
        return _engine


def _select_one():
    from sqlalchemy import text
    return text("SELECT 1")


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)
    return _session_factory


@contextmanager
def get_session() -> Iterator[Session]:
    """Context-managed Session. Commits on success, rolls back on exception."""
    sess = get_session_factory()()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def init_db(drop_existing: bool = False) -> None:
    """Create all tables. Used in tests and first-run development.

    Production uses Alembic migrations (see alembic/), NOT this function.
    """
    from curaniq.db.models import Base
    eng = get_engine()
    if drop_existing:
        Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)


def reset_engine_for_tests() -> None:
    """Tests that change CURANIQ_DATABASE_URL must call this to rebuild."""
    global _engine, _session_factory
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:
            pass
    _engine = None
    _session_factory = None
