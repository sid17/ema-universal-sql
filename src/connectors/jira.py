"""The mock Jira issues connector.

The structural half lives in
:class:`~src.connectors.mock_adapter.MockConnectorAdapter`; this class carries
only what is Jira-shaped. The capability model is loaded from
``connectors.capabilities`` (seeded from ``config/connectors/jira.yaml``).

Jira's shape, and why it is the *interesting* one:

- Every filter is **optional** — unlike GitHub's required ``repo`` — so this
  adapter proves the contract handles both requirements, not just one.
- ``updated`` supports **range operators** (``>``, ``>=``, ``<``, ``<=``), where
  every other column is equality only. That asymmetry is what makes
  ``CapabilityModel.supports(column, op)`` a two-argument question instead of a
  membership test.
- Pagination is **offset**-style (``startAt`` / ``total``), so the two adapters
  between them exercise both strategies.
- This is the source that carries ``assignee`` (the RLS subject) and
  ``reporter_email`` (the CLS-masked column), so Phase 2's entitlement work
  lands here.
"""

from typing import Any

from src.connectors.mock_adapter import MockConnectorAdapter
from src.connectors.mock_data import jira_rows


class JiraConnectorAdapter(MockConnectorAdapter):
    connector_type = "jira"
    resource = "issues"

    #: Deliberately the slow one. Jira carries the RLS subject (``assignee``)
    #: and the CLS-masked column (``reporter_email``), so "the entitled source
    #: is also the expensive one" is the shape a reviewer should read off the
    #: waterfall — and it is what makes "P95 was Jira, not the engine" a
    #: finding rather than a caption (ADR-038).
    simulated_latency_ms = 180.0

    def dataset(self) -> list[dict[str, Any]]:
        return jira_rows()
