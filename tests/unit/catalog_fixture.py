"""The two sources the unit tests parse, plan and execute against.

Extracted from `conftest.py` when that file reached LAW 1's 400-line decompose
threshold. These are *source definitions* — the capability dicts an admin would
put in `config/connectors/*.yaml`, plus the catalog built from them. They are
data, not fixtures; `conftest` wraps the catalog in one.
"""

from src.connectors.base import CapabilityModel
from src.connectors.request import EndpointSpec, compose_endpoint
from src.connectors.response import RateLimitDialect
from src.sqlparse.catalog import Source, SourceCatalog

#: The `api:` blocks from `config/connectors/*.yaml`, in the same shape the
#: seeder reads. Composed through `compose_endpoint` below rather than written
#: out pre-composed, so these fixtures exercise the identical path `make seed`
#: takes — a divergence there would otherwise only show up in integration.
GITHUB_API = {
    "host": "api.github.com",
    "auth_scheme": "bearer",
    "headers": {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    },
    "rate_limit": {
        "limit_header": "X-RateLimit-Limit",
        "remaining_header": "X-RateLimit-Remaining",
        "reset_header": "X-RateLimit-Reset",
        "exhausted_status": 403,
        "sends_retry_after": False,
        "extra_headers": {"X-RateLimit-Resource": "core"},
        "exhausted_body": {
            "message": "API rate limit exceeded for installation.",
            "documentation_url": (
                "https://docs.github.com/rest/overview/rate-limits-for-the-rest-api"
            ),
        },
    },
}

JIRA_API = {
    "host": "ema.atlassian.net",
    "auth_scheme": "basic",
    "headers": {"Accept": "application/json"},
    "rate_limit": {
        "limit_header": "X-RateLimit-Limit",
        "remaining_header": "X-RateLimit-Remaining",
        "reset_header": "X-RateLimit-Reset",
        "exhausted_status": 429,
        "sends_retry_after": True,
        "exhausted_body": {"errorMessages": ["Rate limit exceeded."], "errors": {}},
    },
}

GITHUB_ENDPOINT = EndpointSpec.from_dict(
    compose_endpoint(GITHUB_API, {"method": "GET", "path": "/repos/{repo}/pulls"})
)
JIRA_ENDPOINT = EndpointSpec.from_dict(
    compose_endpoint(
        JIRA_API,
        {"method": "GET", "path": "/rest/api/3/search", "expression_param": "jql"},
    )
)

GITHUB_RATE_LIMIT = RateLimitDialect.from_dict(GITHUB_API["rate_limit"])
JIRA_RATE_LIMIT = RateLimitDialect.from_dict(JIRA_API["rate_limit"])

GITHUB_CAPABILITIES = {
    "columns": [
        "number",
        "title",
        "author",
        "repo",
        "state",
        "issue_key",
        "created_at",
        "updated_at",
    ],
    "key_columns": {
        "repo": {
            "require": "required",
            "ops": ["="],
            "option": {"inject_into": "path", "field": "repo"},
        },
        "state": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "state"},
        },
        "author": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "author"},
        },
    },
    "column_types": {"number": "integer"},
    "sortable": ["created_at", "updated_at"],
    "pagination": {
        "strategy": "cursor",
        "page_size": 100,
        "token_option": {"inject_into": "query", "field": "cursor"},
        "size_option": {"inject_into": "query", "field": "per_page"},
        "stop": "returned<page_size",
    },
}

JIRA_CAPABILITIES = {
    "columns": ["key", "status", "assignee", "reporter_email", "project", "updated"],
    "key_columns": {
        "status": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "jql", "field": "status"},
        },
        "assignee": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "jql", "field": "assignee"},
        },
        "project": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "jql", "field": "project"},
        },
        "updated": {
            "require": "optional",
            "ops": ["=", ">", ">=", "<", "<="],
            "option": {"inject_into": "jql", "field": "updated"},
        },
    },
    "sortable": ["updated"],
    "pagination": {
        "strategy": "offset",
        "page_size": 100,
        "token_option": {"inject_into": "query", "field": "startAt"},
        "size_option": {"inject_into": "query", "field": "maxResults"},
        "stop": "returned<page_size",
    },
}


def build_catalog() -> SourceCatalog:
    """The two sources, built from the same capability dicts the adapters use."""
    return SourceCatalog(
        {
            ("github", "pull_requests"): Source(
                connector_type="github",
                resource="pull_requests",
                capabilities=CapabilityModel.from_dict(GITHUB_CAPABILITIES),
            ),
            ("jira", "issues"): Source(
                connector_type="jira",
                resource="issues",
                capabilities=CapabilityModel.from_dict(JIRA_CAPABILITIES),
            ),
        }
    )


def connector_rows() -> list[dict]:
    """What `ControlPlaneRepository.list_connectors()` returns, one row per resource.

    The registry builds both the catalog and the adapters from rows like these,
    so a fake control plane that returns them exercises the real wiring rather
    than a shortcut around it.
    """
    return [
        {
            "connector_type": "github",
            "resource": "pull_requests",
            "version": "1.0.0",
            "endpoint": compose_endpoint(
                GITHUB_API, {"method": "GET", "path": "/repos/{repo}/pulls"}
            ),
            "rate_limit": GITHUB_API["rate_limit"],
            "capabilities": GITHUB_CAPABILITIES,
        },
        {
            "connector_type": "jira",
            "resource": "issues",
            "version": "1.0.0",
            "endpoint": compose_endpoint(
                JIRA_API,
                {"method": "GET", "path": "/rest/api/3/search", "expression_param": "jql"},
            ),
            "rate_limit": JIRA_API["rate_limit"],
            "capabilities": JIRA_CAPABILITIES,
        },
    ]
