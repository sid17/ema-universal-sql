"""The five scenes. Each one runs the real fetch path and prints what happened."""

from typing import Any

from src.connectorlab import render
from src.connectorlab.wiring import DEFAULT_SCOPE, DEFAULT_TENANT, Lab
from src.connectors.base import FetchRequest
from src.connectors.pagination import decode_token
from src.connectors.request import build_request
from src.models.errors import ApiError

#: A question each source can answer, used across the scenes so the reader
#: compares like with like.
QUESTIONS: dict[str, dict[str, Any]] = {
    "github.pull_requests": {"repo": "ema/core", "state": "open"},
    "jira.issues": {"status": "In Progress", "assignee": "alice"},
}

#: The tenant whose GitHub budget the rate-limit scene drains. NOT the demo
#: tenant: `make demo`'s 429 depends on tenant_acme's bucket being full when it
#: starts, and an artifact generator that quietly breaks another artifact is
#: worse than one that prints less.
THROTTLE_TENANT = "tenant_globex"

#: Small enough that both sources page more than once, large enough that both
#: reach a last page inside PAGE_CAP — the walkthrough has to SHOW `has_more`
#: going false, not just going true.
PAGE_SIZE = 4
PAGE_CAP = 8

#: The pagination scene widens Jira's filter. `assignee = alice` matches three
#: rows, which fits in one page — and a source that never shows a second page
#: demonstrates nothing about pagination.
PAGING_QUESTIONS = QUESTIONS | {"jira.issues": {"status": "In Progress"}}


def request_for(qualified: str, **overrides: Any) -> FetchRequest:
    fields: dict[str, Any] = {
        "tenant_id": DEFAULT_TENANT,
        "entitlement_scope": DEFAULT_SCOPE,
        "predicates": QUESTIONS[qualified],
        "limit": 3,
        "max_staleness_ms": 0,
    }
    return FetchRequest(**(fields | overrides))


async def capabilities(lab: Lab) -> list[str]:
    """What each source can be asked — read from the control plane, not from code."""
    out = render.heading("1. CAPABILITIES", "what this source accepts, and where each value goes")
    out += render.note(
        "Every row below is a seeded row, not a Python constant. An admin adds a "
        "source by writing config/connectors/*.yaml; nothing here is duplicated in code."
    )
    for row in lab.rows:
        qualified = f"{row['connector_type']}.{row['resource']}"
        adapter = lab.adapter(qualified)
        model = adapter.capabilities()
        out += [
            "",
            f"  {qualified}  ->  {adapter.endpoint.method} "
            f"https://{adapter.endpoint.host}{adapter.endpoint.path_template}",
        ]
        out += [""]
        out += render.table(
            ["column", "type", "operators", "required", "injected into"],
            [
                [
                    name,
                    model.type_of(name),
                    " ".join(capability.ops),
                    "yes" if capability.require == "required" else "-",
                    f"{capability.option.inject_into}:{capability.option.field}",
                ]
                for name, capability in model.key_columns.items()
            ],
        )
        page = model.pagination
        out += [
            "",
            f"  pagination   {page.strategy}, {page.page_size}/page, "
            f"token -> {page.token_option.inject_into}:{page.token_option.field}, "
            f"size -> {page.size_option.field}",
            f"  sortable     {', '.join(page and model.sortable) or '-'}",
            f"  refuses with {adapter.rate_limit.exhausted_status}"
            f"{' + Retry-After' if adapter.rate_limit.sends_retry_after else ''}",
        ]
    return out


async def request(lab: Lab) -> list[str]:
    """One FetchRequest, two very different calls."""
    out = render.heading("2. THE CALL WE WOULD SEND", "a FetchRequest, compiled")
    out += render.note(
        "Built by src/connectors/request.py from the capability model alone. "
        "GitHub puts `repo` in the path and `state` in the query; Jira composes every "
        "filter into one JQL expression. Neither adapter special-cases the other."
    )
    for qualified, predicates in QUESTIONS.items():
        adapter = lab.adapter(qualified)
        fetch_request = request_for(qualified)
        outbound = build_request(
            adapter.endpoint, adapter.capabilities(), fetch_request, credential="(resolved)"
        )
        out += ["", f"  in:  {dict(predicates)} limit={fetch_request.limit}", ""]
        out += render.request_block(outbound)
    return out


async def fetch(lab: Lab) -> list[str]:
    """Request in, source response out, rows back. Twice, so `served` flips."""
    out = render.heading("3. INPUT AND OUTPUT", "the real fetch, end to end")
    for qualified in QUESTIONS:
        adapter = lab.adapter(qualified)
        live = await adapter.fetch(request_for(qualified))
        out += ["", f"  --- {qualified} ---", ""]
        out += render.request_block(adapter.last_request)
        out += ["", *render.response_block(adapter.last_response)]
        out += ["", "  parsed rows (field mapping applied):", *render.rows_block(live.rows)]
        out += [
            "",
            f"  served={live.served}  has_more={live.has_more}  "
            f"etag={live.etag}  rows={len(live.rows)}",
        ]
        cached = await adapter.fetch(request_for(qualified, max_staleness_ms=60_000))
        out += [f"  same question again -> served={cached.served} (no token spent)"]
    return out


async def paginate(lab: Lab) -> list[str]:
    """Walk each source to its last page, and show what the token really is."""
    out = render.heading("4. PAGINATION", "one contract, two strategies")
    out += render.note(
        "The token this system hands out is opaque and names the strategy that issued "
        "it. What goes ON THE WIRE is the source's own form: GitHub gets its cursor "
        "back verbatim, Jira gets an integer startAt it can actually understand."
    )
    for qualified, predicates in PAGING_QUESTIONS.items():
        adapter = lab.adapter(qualified)
        strategy = adapter.capabilities().pagination.strategy
        out += ["", f"  --- {qualified} ({strategy}) ---", ""]
        walked, page, number = [], None, 1
        while number <= PAGE_CAP:
            result = await adapter.fetch(
                request_for(qualified, predicates=predicates, limit=PAGE_SIZE, page=page)
            )
            sent = adapter.last_request.query
            token_field = adapter.capabilities().pagination.token_option.field
            walked.append(
                [
                    str(number),
                    f"{token_field}={sent.get(token_field, '(omitted)')}",
                    str(len(result.rows)),
                    "yes" if result.has_more else "no",
                    (
                        f"offset {decode_token(result.next_cursor, strategy)}"
                        if result.next_cursor
                        else "(none — last page)"
                    ),
                ]
            )
            if not result.has_more:
                break
            page, number = result.next_cursor, number + 1
        out += render.table(
            ["page", "paging param sent", "rows", "more?", "cursor handed back decodes to"],
            walked,
        )

    out += ["", "  --- a token from the wrong source ---", ""]
    out += render.note(
        "A GitHub cursor is a valid integer offset once decoded, so handing one to Jira "
        "would return real rows for the wrong question. The token names its own issuer, "
        "so it is refused instead."
    )
    github = lab.adapter("github.pull_requests")
    first = await github.fetch(request_for("github.pull_requests", limit=PAGE_SIZE))
    try:
        await lab.adapter("jira.issues").fetch(request_for("jira.issues", page=first.next_cursor))
    except ValueError as refused:
        out += ["", f"  refused: {refused}"]
    else:  # pragma: no cover - a regression in the token's self-description
        out += ["", "  NOT REFUSED — the cursor's strategy tag is not being checked"]
    return out


async def ratelimit(lab: Lab) -> list[str]:
    """Drain a real bucket and let the source say so in its own words."""
    out = render.heading("5. RATE LIMIT", "the source's dialect, our vocabulary")
    out += render.note(
        "The X-RateLimit-* numbers are not computed for display: they ARE the token "
        "bucket's decision, returned by the same Redis call that spent the token. The "
        "source's reported budget and the query envelope's rate_limit_status cannot drift."
    )
    out += render.note(
        f"Draining {THROTTLE_TENANT}, not the demo tenant — `make demo` needs "
        f"tenant_acme's GitHub bucket full when it starts."
    )
    adapter = lab.adapter("github.pull_requests")
    spent, refusal = 0, None
    for attempt in range(60):
        try:
            await adapter.fetch(
                request_for(
                    "github.pull_requests",
                    tenant_id=THROTTLE_TENANT,
                    entitlement_scope=f"{DEFAULT_SCOPE}:{attempt}",
                )
            )
            spent += 1
        except ApiError as exhausted:
            refusal = exhausted
            break

    out += ["", f"  {spent} live fetches succeeded, then:", ""]
    if refusal is None:
        out += ["  the bucket did not empty — budget raised since this was written?"]
        return out
    out += render.response_block(adapter.last_response)
    out += [
        "",
        "  normalised for the caller:",
        f"    error_code    {refusal.code.value}",
        f"    http          {refusal.http}",
        f"    retry_after   {refusal.retry_after_ms}ms",
    ]
    out += render.note(
        "GitHub answers 403 with the remaining count at zero; Jira answers 429 with a "
        "Retry-After. Both arrive here as RATE_LIMIT_EXHAUSTED — absorbing that "
        "difference is the connector's job."
    )
    return out


SCENES = {
    "capabilities": capabilities,
    "request": request,
    "fetch": fetch,
    "paginate": paginate,
    "ratelimit": ratelimit,
}
