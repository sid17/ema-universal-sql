# Artifacts

Every file here is **generated**, never hand-edited. Regenerate with `make artifacts`
(demo + trace + scrape) or the individual targets below.

| Folder | Target | Contents |
|---|---|---|
| `load/` | `make load-mt` | k6 summaries — throughput, latency, measured cache hit ratio, tenant-leak count |
| `trace/` | `make trace` | one request's stage waterfall (`.txt`, `.svg`, `.png`) |
| `metrics/` | `make scrape` | `GET /metrics` with real samples |
| `demo/` | `make demo` | the scripted walkthrough, teed verbatim |

Run `make scrape` **after** a demo or load run — the histograms are empty and the
`rate_limit_remaining` gauge has no samples until a query has executed.

See [`../LOAD-TESTING.md`](../LOAD-TESTING.md) for the load-test method and
[`../LOAD-RESULTS.md`](../LOAD-RESULTS.md) for what the numbers mean.
