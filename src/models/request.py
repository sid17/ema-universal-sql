"""The request body accepted by ``POST /v1/query``."""

from pydantic import BaseModel


class QueryRequest(BaseModel):
    """One cross-app SQL query plus its freshness and pagination controls."""

    sql: str
    max_staleness_ms: int = 60_000
    cursor: str | None = None
