"""Loading `config/connectors/*.yaml` into the global catalog.

Split from `scripts/seed.py` at LAW 1's 400-line decompose threshold, and it is
a natural seam: every other section of the seeder writes **per-tenant** rows
(grants, secrets, policies, budgets), while this one writes the single global
catalog every tenant shares (ADR-013).
"""

from pathlib import Path
from typing import Any

import yaml
from psycopg.types.json import Jsonb

from src.connectors.request import compose_endpoint

#: The fields a connector file must declare at the top level, and inside `api:`.
REQUIRED_TOP_LEVEL = ("connector_type", "version", "api", "resources")
REQUIRED_API = ("host", "auth_scheme", "rate_limit")
REQUIRED_RESOURCE = ("endpoint", "capabilities")


#: `config/`, resolved from this file so the seeder works from any cwd.
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class SeedError(RuntimeError):
    """Seeding failed. Never swallowed — a half-seeded control plane is worse
    than an unseeded one, because the failures it causes look like app bugs."""


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SeedError(f"missing config file: {path}")
    with path.open() as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise SeedError(f"{path} must contain a YAML mapping, got {type(document).__name__}")
    return document


def seed_connectors(conn) -> int:
    """The global connector catalog — one YAML file per connector (ADR-013).

    **One row per RESOURCE, not per file.** A connector file describes an API
    (`api:`) and the calls it serves (`resources:`), so onboarding another
    GitHub endpoint is one more entry under `resources:` and no Python at all.
    Returns the number of rows written, which is what the summary reports.
    """
    files = sorted((CONFIG_DIR / "connectors").glob("*.yaml"))
    if not files:
        raise SeedError(f"no connector definitions found in {CONFIG_DIR / 'connectors'}")

    written = 0
    with conn.cursor() as cur:
        for path in files:
            written += _seed_one_connector(cur, path, load_yaml(path))
    return written


def _seed_one_connector(cur, path: Path, doc: dict[str, Any]) -> int:
    """Write one row per resource declared in one connector file."""
    for field in REQUIRED_TOP_LEVEL:
        if field not in doc:
            raise SeedError(f"{path} is missing required field {field!r}")

    api = doc["api"]
    for field in REQUIRED_API:
        if field not in api:
            raise SeedError(f"{path} is missing required field 'api.{field}'")

    resources = doc["resources"]
    if not resources:
        raise SeedError(f"{path} declares no resources; a connector serves at least one API call")

    for resource, spec in resources.items():
        for field in REQUIRED_RESOURCE:
            if field not in spec:
                raise SeedError(f"{path} resource {resource!r} is missing {field!r}")
        if "path" not in spec["endpoint"]:
            raise SeedError(f"{path} resource {resource!r} has no endpoint.path")
        cur.execute(
            "INSERT INTO connectors "
            "  (connector_type, resource, version, endpoint, rate_limit, capabilities) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (connector_type, resource) DO UPDATE SET "
            "  version = EXCLUDED.version, endpoint = EXCLUDED.endpoint, "
            "  rate_limit = EXCLUDED.rate_limit, capabilities = EXCLUDED.capabilities",
            (
                doc["connector_type"],
                resource,
                str(doc["version"]),
                Jsonb(compose_endpoint(api, spec["endpoint"])),
                Jsonb(api["rate_limit"]),
                Jsonb(spec["capabilities"]),
            ),
        )
    return len(resources)
