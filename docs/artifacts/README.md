# Artifacts

Every file here is **generated**, never hand-edited. Regenerate with `make artifacts`
(demo + demo-detail + trace + connectors + scrape) or the individual targets below.

| Folder | Target | Contents |
|---|---|---|
| `load/` | `make load-mt` | k6 summaries — throughput, latency, measured cache hit ratio, tenant-leak count |
| `trace/` | `make trace` | one request's stage waterfall (`.txt`, `.svg`, `.png`) |
| `metrics/` | `make scrape` | `GET /metrics` with real samples |
| `demo/` | `make demo` | `demo-output.txt` — the scripted walkthrough, teed verbatim. **The 60-second read:** four calls, four hard parts, a narrative. |
| `demo/` | `make demo-detail` | `demo-detail.txt` — the coverage matrix. **The checklist:** ~34 scenes answering the brief's detailed requirement list line by line, so every published capability has the call that reaches it. |
| `connectors/` | `make connectors` | what goes in and out of each connector — the request it would send, the response it parses, pagination, and the rate-limit refusal |

The two demo artifacts are split by **job**, not by size: one answers "does this work?", the
other answers "does it do *everything* asked?". Neither should grow into the other.

`make connectors` queries `tenant_load` and drains `tenant_globex`, never `tenant_acme` —
`make demo` needs acme's 5+2 GitHub budget intact for its 429 to be deterministic.
`make demo-detail` *does* drain acme deliberately (§3 is the rate-limit section), but every
one of its sections resets first, so it reproduces from any starting state.

Run `make scrape` **after** a demo or load run — the histograms are empty and the
`rate_limit_remaining` gauge has no samples until a query has executed.

See [`../LOAD-TESTING.md`](../LOAD-TESTING.md) for the load-test method and
[`../LOAD-RESULTS.md`](../LOAD-RESULTS.md) for what the numbers mean.
