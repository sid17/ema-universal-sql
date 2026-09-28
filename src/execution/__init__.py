"""Stage 5 — fetch in parallel, join in DuckDB, then assemble the envelope.

Two modules, not one (ADR-034). :mod:`src.execution.federation` knows DuckDB and
nothing about the envelope; :mod:`src.execution.assemble` knows the response
contract and nothing about DuckDB.
"""
