"""Mock connectors behind one uniform contract.

``BaseConnectorAdapter.fetch()`` is the single seam a future *live* adapter
reimplements (HLD §7, ADR-007). Everything above it — the SQL pipeline in
Phase 2 — is written against the contract in :mod:`src.connectors.base` and
never against a mock's internals.
"""
