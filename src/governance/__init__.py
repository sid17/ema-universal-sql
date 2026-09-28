"""Governance primitives: the token bucket, the freshness cache, and secrets.

These three are what make the connectors in ``src/connectors/`` safe to share
between tenants. They know nothing about HTTP or SQL — each takes an
already-resolved key and returns a decision.
"""
