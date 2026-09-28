"""Mock connectors behind one uniform contract.

``BaseConnectorAdapter.fetch()`` is the single seam a future *live* adapter
reimplements. Everything above it — the whole SQL pipeline — is written against
the contract in :mod:`src.connectors.base` and never against a mock's
internals.
"""
