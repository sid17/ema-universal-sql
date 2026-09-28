"""Eight workers, one database, one migration run.

`CMD ["uvicorn", "--workers", "8"]` means eight processes call `run_migrations`
within milliseconds of each other on a cold start. Postgres's
`CREATE TABLE IF NOT EXISTS` is **not** race-safe — two concurrent creates both
pass the existence check and one fails with
`UniqueViolation on pg_type_typname_nsp_index`.

That is not hypothetical. On a fresh volume, seven of eight workers died with
"Application startup failed. Exiting."; uvicorn respawned them, the retry
succeeded, and the stack came up healthy in 4s while dumping seven tracebacks
into the log of every fresh clone.

Threads rather than processes: the failure is in Postgres, not in Python, so
concurrent *sessions* are what reproduces it — and each thread opens its own
pool, which is what eight uvicorn workers do.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.control_plane.db import create_pool, run_migrations

DATABASE_URL = os.environ.get("SEED_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason="Set SEED_TEST_DATABASE_URL (or run `make test-integration`).",
)

#: Matches the image's default worker count, so this reproduces the real shape.
WORKERS = 8


def migrate_once() -> list[str]:
    """One worker's whole startup path: its own pool, then the migration run."""
    pool = create_pool(DATABASE_URL)
    try:
        return run_migrations(pool)
    finally:
        pool.close()


def test_concurrent_startups_do_not_race_on_the_ledger():
    """**GATE.** Eight simultaneous runs, zero exceptions.

    Without the advisory lock this raises `UniqueViolation` from whichever
    threads lost the `CREATE TABLE IF NOT EXISTS` race.
    """
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = [
            future.result() for future in [pool.submit(migrate_once) for _ in range(WORKERS)]
        ]

    assert len(results) == WORKERS


def test_a_migration_is_applied_by_exactly_one_worker():
    """Serialising is not enough — it must also not apply anything twice.

    The schema is already current when this runs, so every worker should report
    an empty list. A non-empty one would mean a file was re-applied, which for a
    migration like 003 (which drops and recreates `connectors`) would wipe the
    catalog out from under a running app.
    """
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = [
            future.result() for future in [pool.submit(migrate_once) for _ in range(WORKERS)]
        ]

    assert all(applied == [] for applied in results), (
        f"a migration was re-applied on a current schema: {results}"
    )


def test_the_lock_is_released_so_a_later_startup_is_not_blocked():
    """A session lock that leaked would hang the next `make up` forever, which
    is a far worse failure than the tracebacks it replaced."""
    migrate_once()
    assert migrate_once() == []
