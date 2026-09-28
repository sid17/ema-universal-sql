"""Observability seam: OTel stage spans and the Prometheus registry.

Each pipeline stage is annotated with `@stage_span`, and the envelope's
`trace_id` comes from `current_trace_id()`.
"""
