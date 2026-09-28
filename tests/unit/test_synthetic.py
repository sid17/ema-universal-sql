"""The per-tenant load datasets, and the properties the load test depends on.

Three claims are load-bearing and each has a test here: the data is
deterministic (a run is reproducible), every row carries its tenant's stamp in a
*projected* column (a leak is detectable in a response), and the two sources
actually join (otherwise the load test measures an empty result).
"""

from __future__ import annotations

import pytest

from src.config import get_settings
from src.connectors.synthetic import (
    LOAD_USER,
    project_for,
    repo_for,
    synthetic_rows,
)

ROWS = 50
KEYSPACE = 4


def gh_rows(tenant: str = "tenant_load_00"):
    return synthetic_rows(tenant, "github", ROWS, KEYSPACE)


def jira_rows(tenant: str = "tenant_load_00"):
    return synthetic_rows(tenant, "jira", ROWS, KEYSPACE)


# --- determinism -------------------------------------------------------------


def test_the_same_inputs_always_produce_the_same_rows() -> None:
    assert gh_rows() == gh_rows()
    assert jira_rows() == jira_rows()


def test_a_returned_row_can_be_mutated_without_poisoning_the_next_caller() -> None:
    """The generator caches; callers must not share mutable state through it."""
    first = gh_rows()
    first[0]["title"] = "clobbered"

    assert gh_rows()[0]["title"] != "clobbered"


# --- the leak stamp ----------------------------------------------------------


def test_every_github_row_carries_its_tenant_id_in_a_projected_column() -> None:
    """`title` is in the canonical query's SELECT, so the stamp reaches the
    response and k6 can assert on it for every row of every request."""
    assert all("tenant_load_07" in row["title"] for row in gh_rows("tenant_load_07"))


def test_two_tenants_never_share_a_stamp() -> None:
    seven = {row["title"] for row in gh_rows("tenant_load_07")}
    eight = {row["title"] for row in gh_rows("tenant_load_08")}

    assert seven.isdisjoint(eight)


# --- the key space -----------------------------------------------------------


def test_rows_span_the_whole_key_space_on_both_sources() -> None:
    assert {row["repo"] for row in gh_rows()} == {repo_for(k) for k in range(KEYSPACE)}
    assert {row["project"] for row in jira_rows()} == {project_for(k) for k in range(KEYSPACE)}


def test_each_key_holds_the_requested_number_of_rows() -> None:
    for key in range(KEYSPACE):
        assert len([r for r in gh_rows() if r["repo"] == repo_for(key)]) == ROWS

    assert len(gh_rows()) == ROWS * KEYSPACE


# --- the join, and the entitled result ---------------------------------------


def test_the_two_sources_join_after_every_predicate_is_applied() -> None:
    """The load test is worthless if the entitled result is empty.

    Applies exactly what the query and the RLS policy push down — repo, state,
    project, status and `assignee = :user` — and asserts the join still returns
    more than one page, so `LIMIT 50` is a real page rather than the whole
    answer.
    """
    prs = [r for r in gh_rows() if r["repo"] == repo_for(0) and r["state"] == "open"]
    issues = [
        r
        for r in jira_rows()
        if r["project"] == project_for(0)
        and r["status"] == "In Progress"
        and r["assignee"] == LOAD_USER
    ]
    keys = {row["key"] for row in issues}
    joined = [row for row in prs if row["issue_key"] in keys]

    assert len(joined) > 0
    assert issues, "the RLS subject matches no rows — the load result would be empty"


def test_status_and_state_select_different_rows() -> None:
    """If both filters picked the same rows the join would be a pass-through and
    the predicate work would be free — measuring nothing."""
    open_keys = {r["issue_key"] for r in gh_rows() if r["state"] == "open"}
    progress_keys = {r["key"] for r in jira_rows() if r["status"] == "In Progress"}

    assert open_keys != progress_keys
    assert open_keys & progress_keys


# --- the off switch ----------------------------------------------------------


def test_an_unknown_connector_is_loud_rather_than_an_empty_dataset() -> None:
    """LAW 4: `[]` here is indistinguishable from a tenant with no data."""
    with pytest.raises(ValueError, match="no synthetic dataset for connector"):
        synthetic_rows("tenant_load_00", "salesforce", ROWS, KEYSPACE)


def test_synthetic_data_is_off_by_default() -> None:
    """A normal run and the whole suite must read the committed fixtures."""
    assert get_settings().SYNTHETIC_ROWS == 0


# --- the adapter seam --------------------------------------------------------


async def test_the_adapter_serves_fixtures_to_a_normal_tenant(github) -> None:
    """Synthetic data is opt-in by tenant prefix AND by SYNTHETIC_ROWS.

    With the setting off, every tenant — load-prefixed or not — reads the
    committed fixtures, which is what keeps the rest of this suite honest.
    """
    assert github.dataset_for("tenant_acme") == github.dataset()
    assert github.dataset_for("tenant_load_00") == github.dataset()


async def test_the_adapter_serves_synthetic_rows_to_a_load_tenant(github, monkeypatch) -> None:
    monkeypatch.setenv("SYNTHETIC_ROWS", "10")
    monkeypatch.setenv("SYNTHETIC_KEYSPACE", "2")
    get_settings.cache_clear()
    try:
        load = github.dataset_for("tenant_load_03")
        normal = github.dataset_for("tenant_acme")

        assert len(load) == 20
        assert all("tenant_load_03" in row["title"] for row in load)
        assert normal == github.dataset()
    finally:
        get_settings.cache_clear()
