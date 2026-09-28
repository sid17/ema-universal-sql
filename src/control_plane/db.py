"""Postgres connection pool and the migration runner.

The pool is created once in the app lifespan (`src/main.py`) and hung on
`app.state`, so every later phase's governance module borrows the same
connections rather than opening its own.

`run_migrations()` is idempotent: applied filenames are recorded in a
`schema_migrations` table and skipped on the next run, which is what makes
`make up` safe to re-run against a warm Postgres volume.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from psycopg_pool import ConnectionPool

from src.config import settings

logger = logging.getLogger(__name__)

# src/control_plane/db.py -> src/control_plane -> src -> repo root
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def create_pool(
    dsn: str | None = None,
    *,
    min_size: int = 1,
    max_size: int = 10,
    connect_timeout: float = 30.0,
) -> ConnectionPool:
    """Open a connection pool against ``dsn`` (default: ``DATABASE_URL``).

    Waits for the first connection so a caller that gets a pool back knows
    Postgres is actually reachable — a pool that opens lazily would turn a bad
    DSN into a failure inside the first request instead of at startup.
    """
    pool = ConnectionPool(
        conninfo=dsn or settings.DATABASE_URL,
        min_size=min_size,
        max_size=max_size,
        open=False,
    )
    pool.open(wait=True, timeout=connect_timeout)
    return pool


#: Advisory-lock key for the migration run. Any constant works; this one is
#: `hash("universal_sql.migrations")` truncated, written as a literal so nothing
#: can recompute it differently on another Python build.
MIGRATION_LOCK_KEY = 7_242_119_045_113_622


@contextmanager
def _migration_lock(pool: ConnectionPool) -> Iterator[None]:
    """Serialise migration runs across every process sharing this database.

    `CMD ["uvicorn", "--workers", "8"]` means eight processes call
    :func:`run_migrations` within milliseconds of each other on a cold start.
    Postgres's ``CREATE TABLE IF NOT EXISTS`` is **not** race-safe: two
    concurrent creates both pass the existence check and one fails with
    ``UniqueViolation on pg_type_typname_nsp_index``. Seven of eight workers
    then died with "Application startup failed", uvicorn respawned them, and the
    retry succeeded — so the stack came up healthy while dumping seven
    tracebacks into the log of every fresh clone.

    A session-level advisory lock costs one round trip and makes the whole run
    mutually exclusive. It is held on ONE connection for the duration, because
    a session lock belongs to the session that took it; taking and releasing it
    on pooled connections would unlock nothing.
    """
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            yield
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))


def _applied_filenames(pool: ConnectionPool) -> set[str]:
    """Return the migrations already recorded, creating the ledger if absent."""
    with pool.connection() as conn:
        conn.execute(_SCHEMA_MIGRATIONS_DDL)
        rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def _apply_one(pool: ConnectionPool, path: Path) -> None:
    """Apply a single migration file and record it, in one transaction.

    psycopg runs a multi-statement script in a single ``execute()`` as long as no
    parameters are bound, so the file lands whole or not at all.
    """
    sql = path.read_text(encoding="utf-8")
    try:
        with pool.connection() as conn:
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
    except Exception as exc:
        # Never swallowed: a half-migrated database must fail startup loudly
        # rather than serve requests against a schema nobody can describe.
        logger.error("migration %s failed: %s", path.name, exc)
        raise RuntimeError(f"migration {path.name} failed") from exc


def run_migrations(
    pool: ConnectionPool,
    migrations_dir: Path | str = MIGRATIONS_DIR,
) -> list[str]:
    """Apply every unapplied ``*.sql`` file in sorted filename order.

    Returns the filenames applied by this call — empty when the schema is
    already current.
    """
    directory = Path(migrations_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {directory}")

    newly_applied: list[str] = []
    # The ledger read is INSIDE the lock too, not just the writes: it is the
    # statement that creates `schema_migrations`, and it was the one that raced.
    with _migration_lock(pool):
        applied = _applied_filenames(pool)

        for path in sorted(directory.glob("*.sql"), key=lambda p: p.name):
            if path.name in applied:
                logger.debug("migration %s already applied, skipping", path.name)
                continue
            _apply_one(pool, path)
            logger.info("migration %s applied", path.name)
            newly_applied.append(path.name)

    if not newly_applied:
        logger.info("schema already current (%d migration(s) on record)", len(applied))
    return newly_applied
