"""Stage 5 — fetch in parallel, join in DuckDB, then assemble the envelope.

The split ADR-034 asked for is between *execution* and *the response contract*:
:mod:`src.execution.assemble` knows the envelope and nothing about DuckDB, and
the execution side knows DuckDB and nothing about the envelope.

Phase 4's instrumentation pushed the execution side past LAW 1's 400-line
decompose threshold, so it is now two modules rather than one — along a seam
that was already there. :mod:`src.execution.federation` owns the parallel fetch,
the per-source deadline and partial failure; :mod:`src.execution.join` owns the
Arrow registration and the DuckDB execution. They share only the ``SourceFetch``
records that pass between them.
"""
