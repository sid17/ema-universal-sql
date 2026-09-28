#!/usr/bin/env bash
#
# The coverage matrix. A second walkthrough that answers the brief's detailed
# Prototype Requirements list line by line, as input -> output.
#
# This is NOT scripts/demo.sh and does not replace it. They have different jobs:
#
#   demo-output.txt  narrative, 4 calls   "does this work?"        60 seconds
#   demo-detail.txt  matrix, ~30 scenes   "does it do EVERYTHING?" ticking boxes
#
# Requires: the stack up (`make up`), seeded (`make seed`), and TEST_MODE=1 —
# the /v1/test/* hooks are 404 without it and `set -e` will abort at the first
# reset, loudly, which is the correct failure. `make demo-detail` handles it.
#
# bash + curl + python3 only, like demo.sh: a reviewer runs this on a fresh
# clone and it adds no dependency.
#
# Scenes PRINT. They do not assert (D7) — tests/integration/ owns assertions. A
# drifting fixture should change this artifact, not fail the demo, or a reviewer
# gets a stack trace where an answer belongs.

set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
OUT="${DEMO_DETAIL_OUTPUT:-docs/artifacts/demo/demo-detail.txt}"
PY="${PYTHON:-.venv/bin/python}"
mkdir -p "$(dirname "$OUT")"

HDRS="$(mktemp)"; BODY="$(mktemp)"
trap 'rm -f "$HDRS" "$BODY"' EXIT

SCENES=0
SECTIONS=0

# --- the queries under test --------------------------------------------------
# CANONICAL is byte-identical to scripts/demo.sh's. The
# 3 -> 1 -> 0 proof rests on alice, bob and carol receiving the SAME string, so
# it lives in one variable and is never retyped.
CANONICAL="SELECT pr.title, pr.author, issue.key, issue.status
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 50"

CLS_DEMO="SELECT pr.title, pr.author, issue.key, issue.status, issue.reporter_email
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 50"

SINGLE="SELECT pr.number, pr.title, pr.state, pr.author
FROM   github.pull_requests pr
WHERE  pr.repo = 'ema/core' AND pr.state = 'open'
ORDER BY pr.number DESC
LIMIT 5"

PAGED="SELECT pr.number, pr.title
FROM   github.pull_requests pr
WHERE  pr.repo = 'ema/core'
ORDER BY pr.number DESC
LIMIT 2"

# --- helpers -----------------------------------------------------------------

rule() { echo "--------------------------------------------------------------------------------"; }

section() {
  SECTIONS=$((SECTIONS + 1))
  echo
  echo "################################################################################"
  echo "#  $1"
  echo "#  $2"
  echo "################################################################################"
}

# Ported from scripts/demo.sh, widened to take role/tenant/scopes — all
# three are already MockTokenRequest fields; demo.sh just hardcodes them.
token() {
  local user="$1" role="$2" tenant="$3" scopes="$4"
  local payload
  payload=$("$PY" - "$user" "$role" "$tenant" "$scopes" <<'EOF'
import json, sys
user, role, tenant, scopes = sys.argv[1:5]
body = {"user": user, "role": role, "tenant": tenant}
if scopes != "__default__":
    body["scopes"] = scopes
print(json.dumps(body))
EOF
)
  curl -fsS -XPOST "$BASE/v1/auth/mock-token" \
       -H 'content-type: application/json' -d "$payload" \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["token"])'
}

reset() { curl -fsS -XPOST "$BASE/v1/test/reset" >/dev/null; }

arm() {  # connector mode — one-shot forced failure
  curl -fsS -XPOST "$BASE/v1/test/fail-next" -H 'content-type: application/json' \
       -d "{\"connector\":\"$1\",\"mode\":\"$2\"}" >/dev/null
}

# No `-f`: most scenes here EXPECT 4xx, and curl's fail-fast would abort the run
# on exactly the outputs the artifact exists to show.
render() {
  local status
  status=$(< "$BODY" "$PY" -c '
import json, sys
raw = sys.stdin.read()
try:
    print(json.dumps(json.loads(raw), indent=2))
except ValueError:
    print(raw)
' 2>/dev/null) || status=$(cat "$BODY")
  echo "$status" | sed 's/^/  /'
}

# `scene ID TITLE EXPECTATION [flags]`. The list of these calls IS the coverage
# claim (D2) — a reviewer reads the scene list alone and knows what is covered.
scene() {
  local id="$1" title="$2" expect="$3"; shift 3
  local user=alice role=support tenant=tenant_acme scopes=__default__
  local sql="$CANONICAL" staleness=0 cursor="" path="/v1/query" note=""

  while [ $# -gt 0 ]; do
    case "$1" in
      --user)      user="$2"; shift 2 ;;
      --role)      role="$2"; shift 2 ;;
      --tenant)    tenant="$2"; shift 2 ;;
      --scopes)    scopes="$2"; shift 2 ;;
      --sql)       sql="$2"; shift 2 ;;
      --staleness) staleness="$2"; shift 2 ;;
      --cursor)    cursor="$2"; shift 2 ;;
      --arm)       arm "${2%%:*}" "${2##*:}"; shift 2 ;;
      --path)      path="$2"; shift 2 ;;
      --note)      note="$2"; shift 2 ;;
      *) echo "scene $id: unknown flag $1" >&2; return 1 ;;
    esac
  done

  SCENES=$((SCENES + 1))
  echo
  rule
  echo "  SCENE $id — $title"
  echo "  expect: $expect"
  [ -n "$note" ] && echo "  note:   $note"
  rule
  echo "  caller: user=$user role=$role tenant=$tenant scopes=$([ "$scopes" = __default__ ] && echo '(default)' || echo "'$scopes'")"
  echo "  POST $path"
  if [ "$path" = "/v1/query" ]; then
    echo "$sql" | sed 's/^/    | /'
    echo "    max_staleness_ms=$staleness${cursor:+ cursor=${cursor:0:24}...}"
  fi
  echo

  local body http
  body=$("$PY" - "$sql" "$staleness" "$cursor" <<'EOF'
import json, sys
sql, staleness, cursor = sys.argv[1], int(sys.argv[2]), sys.argv[3]
payload = {"sql": sql, "max_staleness_ms": staleness}
if cursor:
    payload["cursor"] = cursor
print(json.dumps(payload))
EOF
)
  http=$(curl -sS -o "$BODY" -D "$HDRS" -w '%{http_code}' -XPOST "$BASE$path" \
              -H "authorization: Bearer $(token "$user" "$role" "$tenant" "$scopes")" \
              -H 'content-type: application/json' -d "$body")

  echo "  <- HTTP $http"
  # Retry-After is part of the 429 contract and invisible in a
  # body-only artifact, so it is surfaced whenever the source sends it.
  grep -i '^retry-after:' "$HDRS" | sed 's/^/  <- /' || true
  render
}

header() {
  echo "================================================================================"
  echo "  UNIVERSAL SQL — DEMO COVERAGE MATRIX"
  echo "================================================================================"
  echo "  generated by scripts/demo_detail.sh against $BASE"
  echo "  assumes: make up && make seed, and TEST_MODE=1 (the /v1/test/* hooks)"
  echo
  echo "  Companion to demo-output.txt, not a replacement. That file is the 60-second"
  echo "  narrative; this one answers the brief's detailed requirement list line by"
  echo "  line so a reviewer can tick every box without reading source."
  echo
  echo "  SECTIONS"
  echo "    1. The request/response contract   envelope, join_status, pagination, policy config"
  echo "    2. Entitlements                    token -> scopes/roles -> RLS/CLS, and 3 ways to get no rows"
  echo "    3. Rate limits                     the budget ticking down, the 429, the async reroute"
  echo "    4. Freshness                       live / cache / stale, and what a cache hit costs"
  echo "    5. The error vocabulary            all six ErrorCode values, each with the call that produces it"
  echo "    6. The SQL subset                  five rejections at the boundary, with their messages"
  echo "    7. Observability                   the connector-time metric and a trace_id"
  echo "    8. Load                            pointer to the committed k6 artifact"
  echo
  echo "  ON \"SQL OR PLAN JSON\". The brief allows either; this prototype accepts SQL"
  echo "  only. QueryRequest (src/models/request.py) carries sql, max_staleness_ms and"
  echo "  cursor — there is no plan-JSON entry point. Named here rather than left for"
  echo "  a reviewer to discover: it is a reading of \"or\", not an omission."
  echo
  echo "  Scenes print; they do not assert. Assertions live in tests/integration/."
}

# Read back from the last scene's response, so a scene can feed the next one
# (1.3's cursor walk, and §7's trace_id).
cursor_of() { "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("next_cursor") or "")' < "$BODY"; }
trace_of()  { "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("trace_id") or "")' < "$BODY"; }

# The scene list lives next door and is the thing worth reading. See its header.
# shellcheck source=scripts/demo_detail_scenes.sh
. "$(dirname "$0")/demo_detail_scenes.sh"

run_detail() {
  header
  section_1
  section_2
  section_3
  section_4
  section_5
  section_6
  section_7
  section_8
  echo
  echo "================================================================================"
  echo "  covered: $SCENES scenes across $SECTIONS sections"
  echo "================================================================================"
}

run_detail | tee "$OUT"
echo
echo "wrote $OUT"
