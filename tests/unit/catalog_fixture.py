"""The two sources the unit tests parse, plan and execute against.

Extracted from `conftest.py` when that file reached LAW 1's 400-line decompose
threshold. These are *source definitions* — the capability dicts an admin would
put in `config/connectors/*.yaml`, plus the catalog built from them. They are
data, not fixtures; `conftest` wraps the catalog in one.
"""

from src.connectors.base import CapabilityModel
from src.sqlparse.catalog import Source, SourceCatalog

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
        "stop": "returned<page_size",
    },
}

JIRA_CAPABILITIES = {
    "columns": ["key", "status", "assignee", "reporter_email", "project", "updated"],
    "key_columns": {
        "status": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "status"},
        },
        "assignee": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "assignee"},
        },
        "project": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "project"},
        },
        "updated": {
            "require": "optional",
            "ops": ["=", ">", ">=", "<", "<="],
            "option": {"inject_into": "query", "field": "updated"},
        },
    },
    "sortable": ["updated"],
    "pagination": {
        "strategy": "offset",
        "page_size": 100,
        "token_option": {"inject_into": "query", "field": "startAt"},
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
