"""The mock GitHub pull-requests connector.

Everything structural — the six-step ``fetch()`` order, predicate validation,
pagination, caching — lives in :class:`~src.connectors.mock_adapter.MockConnectorAdapter`.
This class carries only what is genuinely GitHub-shaped.

The capability model is **passed in**, loaded from ``connectors.capabilities``
(seeded from ``config/connectors/github.yaml``), never hard-coded here: if an
adapter could declare its own capabilities, it could disagree with the control
plane about what is filterable, and Phase 2's planner reads the control plane.

GitHub's shape, for reference — the details the YAML encodes:

- ``repo`` is **required** and injected into the **path**. There is no
  ``/pulls`` endpoint that spans every repository, so a fetch without it has no
  URL to call. This is the capability model's ``require: required`` earning its
  keep rather than being decoration.
- ``state`` and ``author`` are optional query parameters.
- Pagination is **cursor**-style (what a real GitHub client reads out of the
  ``Link`` header).
"""

from typing import Any

from src.connectors.mock_adapter import MockConnectorAdapter
from src.connectors.mock_data import github_rows


class GitHubConnectorAdapter(MockConnectorAdapter):
    connector_type = "github"
    resource = "pull_requests"

    #: The faster of the two sources, so the waterfall has a contrast to show
    #: rather than two equal bars (ADR-038).
    simulated_latency_ms = 40.0

    def dataset(self) -> list[dict[str, Any]]:
        return github_rows()
