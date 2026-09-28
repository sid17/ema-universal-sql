"""Observability seam: OTel stage spans and the Prometheus registry.

Phase 0 builds the wiring; Phase 2 annotates each pipeline stage with
`@stage_span` and populates the envelope's `trace_id` from `current_trace_id()`.
"""
