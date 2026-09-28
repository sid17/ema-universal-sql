#!/usr/bin/env bash
#
# The scene list — sourced by scripts/demo_detail.sh, which owns the helpers
# these call (`scene`, `section`, `reset`, `arm`, `rule`).
#
# THIS FILE IS THE DELIVERABLE. The runner is plumbing; the list below is the
# coverage claim, and it is meant to be read on its own: a reviewer should be
# able to scan the `scene` calls and know exactly what is covered without
# running anything or reading any source.
#
# Split from the runner to keep both readable. Every section starts with
# `reset` so the artifact reproduces from any starting state — except §4, which
# must NOT reset between its own scenes (its cache-hit scene depends on the
# entry the live scene wrote).


  # Every section starts from a known state. demo.sh resets once; that is not
  # enough here, because §3 drains tenant_acme's GitHub bucket (capacity 7) and
  # §5 arms one-shot failures. The exception is §4, which must NOT reset between
  # its own scenes — its cache-hit scene depends on the entry the live one wrote.
section_1() {
  section "1. THE REQUEST/RESPONSE CONTRACT" "brief: one SQL query across apps, one envelope back"
  reset
  scene "1.1" "single-source query, no join" \
        "200, join_status 'n/a', one entry in sources[]" \
        --sql "$SINGLE" \
        --note "the cheapest introduction to the envelope's eight fields"

  scene "1.2" "the canonical two-source join" \
        "200, join_status 'complete', both connectors in sources[]" \
        --note "the canonical query every other scene varies"
  TRACE_ID=$(trace_of)

  scene "1.3a" "pagination, page 1 of LIMIT 2" \
        "200, 2 rows, a non-null next_cursor" \
        --sql "$PAGED"
  PAGE1=$(cursor_of)
  scene "1.3b" "pagination, page 2 via that cursor" \
        "200, the NEXT 2 rows — no overlap with page 1" \
        --sql "$PAGED" --cursor "$PAGE1" --staleness 60000
  PAGE2=$(cursor_of)
  scene "1.3c" "pagination, the last page" \
        "200, next_cursor null — the walk terminates" \
        --sql "$PAGED" --cursor "$PAGE2" --staleness 60000

  echo
  rule
  echo "  1.4 — the policy config itself: one RLS rule and one column mask."
  echo "        config/policies.yaml,"
  echo "        which is what §2 below turns on. Printed rather than described: the"
  echo "        ask is to see the config, not only its effect."
  rule
  sed 's/^/  | /' config/policies.yaml

}

section_2() {
  section "2. ENTITLEMENTS" "brief: query-time RLS/CLS, compiled into the plan — never post-filtered"
  reset
  scene "2.1" "alice, support" "200, 3 rows" \
        --user alice \
        --note "the Jira adapter received assignee='alice'; the other rows were never fetched"
  scene "2.2" "bob, support — BYTE-IDENTICAL SQL" "200, exactly 1 row" \
        --user bob \
        --note "same \$CANONICAL shell variable as 2.1; only the compiled plan differs"

  echo
  rule
  echo "  2.3 / 2.5 / 2.6 are THE TRICHOTOMY. Three different reasons you get no"
  echo "  rows, which are unremarkable apart and are the sharpest thing in the"
  echo "  system side by side:"
  echo "     2.3  entitled, nothing matches   -> 200, rows: []"
  echo "     2.5  explicitly denied           -> 403 ENTITLEMENT_DENIED"
  echo "     2.6  no matching allow           -> 200, rows: [] (default-deny)"
  echo "  A system that collapses these into one answer cannot tell a caller"
  echo "  whether to ask someone for access or to change their query."
  rule

  scene "2.3" "carol, support — entitled but empty" "200, 0 rows, NOT an error" \
        --user carol \
        --note "carol owns SUP-31 (In Progress) but its only PR is closed — empty via the join"
  scene "2.4" "alice + reporter_email — column masking" \
        "200, reporter_email is an MD5 digest, columns[].masked true" \
        --sql "$CLS_DEMO" --user alice \
        --note "mask kind 'hash', not 'drop': the column stays in the shape, groupable, unreadable"
  scene "2.5" "auditor role — an explicit deny" \
        "403 ENTITLEMENT_DENIED — deny overrides any matching allow" \
        --user dana --role auditor \
        --note "policy deny-jira-issues-auditor; no predicate = deny every row of the resource"
  scene "2.6" "contractor role — default-deny" \
        "200 with rows: [] — governed resource, no allow matches this role" \
        --user erin --role contractor \
        --note "no fixture seeds this: default-deny IS the absence of a matching allow"
  scene "2.7" "empty scopes — the OAuth gate, before entitlement" \
        "403, missing query:execute" \
        --user alice --scopes "" \
        --note "layer L2 in gateway/deps.py: the token is valid, the scope is not present"
  scene "2.8" "tenant_globex — a connector the tenant was never granted" \
        "CONNECTOR_NOT_ENABLED, caught at the PARSER" \
        --tenant tenant_globex \
        --note "globex has a GitHub grant and no Jira grant; refused before anything is planned or fetched"

}

section_3() {
  section "3. RATE LIMITS" "brief: per-tenant budgets, a clean refusal, and a way forward"
  reset
  echo
  echo "  tenant_acme's GitHub budget is max_requests 5 + burst 2 = capacity 7"
  echo "  (config/rate_limits.yaml). Deliberately tiny so the 429 is deterministic"
  echo "  rather than a matter of timing. Each call below is max_staleness_ms=0, so"
  echo "  every one is a live fetch that spends a token."
  for n in 1 2 3 4 5 6 7 8; do
    scene "3.1.$n" "live call $n of 8 against a capacity-7 bucket" \
          "$([ "$n" -lt 8 ] && echo "200, rate_limit_status.github.remaining ticking down" \
                            || echo "429 RATE_LIMIT_EXHAUSTED + Retry-After + suggested_action")" \
          --sql "$SINGLE"
  done
  scene "3.2" "the async reroute the 429 points at" \
        "501 NOT_IMPLEMENTED — deliberately not 404" \
        --path "/v1/query/async" \
        --note "a suggested_action pointing at a 404 reads as a bug; at a 501 it reads as a scoped decision"

}

section_4() {
  section "4. FRESHNESS" "brief: a staleness knob the caller controls, and honest labelling"
  reset
  scene "4.1" "max_staleness_ms=0 — force a live fetch" \
        "served 'live', stats.connector_ms populated" \
        --sql "$SINGLE" --staleness 0
  echo "  ^^ remember this rate_limit_status; 4.2 must match it exactly."
  scene "4.2" "the SAME query, max_staleness_ms=60000" \
        "served 'cache', connector_ms empty, rate_limit_status UNCHANGED from 4.1" \
        --sql "$SINGLE" --staleness 60000 \
        --note "a cache hit spends no token — that is an equality between two responses, so both are shown"
  scene "4.3" "a staleness budget smaller than the entry's age" \
        "200 plus a STALE_DATA warning — served, and labelled" \
        --sql "$SINGLE" --staleness 1 \
        --note "the knob is a budget, not a switch: the caller is told what they got, not refused"

}

section_5() {
  section "5. THE ERROR VOCABULARY" "six codes, each with the call that produces it"
  reset
  scene "5.1" "SOURCE_TIMEOUT — jira forced to time out" \
        "200 partial: join_status 'incomplete', next_cursor null, a warning naming jira" \
        --arm jira:timeout \
        --note "the full partial envelope: an offset into an incomplete result is not stable, so no cursor"
  scene "5.2" "CONNECTOR_AUTH_ERROR — github credential rejected" \
        "403 — a config error, never presented as partial" \
        --arm github:auth --sql "$SINGLE"
  scene "5.3" "RATE_LIMIT_EXHAUSTED — the source itself throttles us" \
        "the source's 429, distinct from our own bucket in §3" \
        --arm jira:throttled
  scene "5.4" "CONNECTOR_NOT_ENABLED — armed at the connector" \
        "403 — the same code §2.8 reaches via a missing grant" \
        --arm github:not_enabled --sql "$SINGLE"
  echo
  rule
  echo "  code                      HTTP  produced by"
  echo "  SOURCE_TIMEOUT             200  5.1  degraded to partial + a warning. The 504 in"
  echo "                             504       HTTP_STATUS_FOR_CODE applies when a timeout"
  echo "                                       fails the query instead of degrading it."
  echo "  CONNECTOR_AUTH_ERROR       403  5.2  (config — fails the query)"
  echo "  RATE_LIMIT_EXHAUSTED       429  3.1.8 (our bucket) and 5.3 (the source's)"
  echo "  CONNECTOR_NOT_ENABLED      403  2.8  (missing grant) and 5.4 (armed)"
  echo "  ENTITLEMENT_DENIED         403  2.5  an explicit policy deny, and 2.7 a missing"
  echo "                                       scope — both are \"forbidden\", not \"empty\"."
  echo "  STALE_DATA                 200  4.3  (a warning, not a refusal)"
  echo
  echo "  Two failures are deliberately NOT in this vocabulary and carry their own"
  echo "  types instead: 401 UnauthenticatedError (transport) and 400"
  echo "  InvalidQueryError (request shape, §6 below). Diluting the six would"
  echo "  make a domain code mean \"your request was malformed\" instead."
  rule

}

section_6() {
  section "6. THE SQL SUBSET" "the boundary was chosen, not stumbled into — five rejections"
  reset
  scene "6.1" "SELECT *" "400, and a message naming the pushdown reason" \
        --sql "SELECT * FROM github.pull_requests pr WHERE pr.repo = 'ema/core'"
  scene "6.2" "GROUP BY" "400, aggregation is outside the subset" \
        --sql "SELECT pr.author FROM github.pull_requests pr WHERE pr.repo = 'ema/core' GROUP BY pr.author"
  scene "6.3" "a subquery" "400, the subset is projection, filters, joins and LIMIT" \
        --sql "SELECT pr.title FROM github.pull_requests pr WHERE pr.repo = (SELECT pr2.repo FROM github.pull_requests pr2)"
  scene "6.4" "LIKE" "400, use = on an indexed column" \
        --sql "SELECT pr.title FROM github.pull_requests pr WHERE pr.title LIKE '%fix%'"
  scene "6.5" "IN" "400, use = or OR" \
        --sql "SELECT pr.title FROM github.pull_requests pr WHERE pr.state IN ('open', 'closed')"

}

section_7() {
  section "7. OBSERVABILITY" "brief: metrics and one trace showing connector time"
  echo
  echo "  The per-connector fetch histogram from GET /metrics. Per-connector and not"
  echo "  one aggregate, because \"how long did Jira take\" is the question the brief's"
  echo "  trace requirement is actually asking."
  rule
  curl -fsS "$BASE/metrics" | grep -E '^connector_fetch_duration_seconds_(count|sum)' | sed 's/^/  /' || true
  rule
  echo "  rate_limit_remaining, one series per (tenant, connector):"
  curl -fsS "$BASE/metrics" | grep -E '^rate_limit_remaining' | sed 's/^/  /' || true
  rule
  echo "  trace_id from scene 1.2: ${TRACE_ID:-(none)}"
  echo "  The rendered waterfall for a run of that same query is committed at"
  echo "  docs/artifacts/trace/ and regenerated by \`make trace\`. Spans go to"
  echo "  traces/spans.jsonl; there is no tracing backend and no fourth container."

}

section_8() {
  section "8. LOAD" "brief: a throughput number, honestly bounded"
  echo
  echo "  Not re-run here — it takes 60s+, needs the tenant_load fixtures, and the"
  echo "  artifact is already committed. Regenerate with \`make load-mt\`."
  echo "  Headline numbers from docs/artifacts/load/k6-summary.txt:"
  rule
  if [ -f docs/artifacts/load/k6-summary.txt ]; then
    grep -E 'http_req_duration|http_reqs|checks|iterations' docs/artifacts/load/k6-summary.txt \
      | head -8 | sed 's/^/  /' || true
  else
    echo "  (docs/artifacts/load/k6-summary.txt not present — run \`make load\`)"
  fi
  rule

  echo
}
