"""Shapes of the dicts the chat engine passes around: search rows, hits, passage records, grouped documents, the
Vector Search filter and request body, what the retrieval returns and what the answer call reports."""

from typing import Any, Dict, List, TypedDict


class Hit(TypedDict):
    """One way a chunk was found by a query of the turn."""
    q: int | None          # index of the query in the turn's queries, None for a lookup by REF
    via: str               # rerank, raw, code_lookup or ref_lookup
    rank: int              # position in that query's results, from 0
    score: float | None


class SearchRow(TypedDict, total=False):
    """A row of the chunks index, with the hits the engine adds."""
    chunk_id: str
    IDDOC: int | str
    REF: str
    division: str
    url: str
    semantic_headers: str
    chunk_text: str
    score: float
    _hits: List[Hit]


class DocumentPassages(TypedDict):
    """The passages of one document, as handed to the answer prompt."""
    url: str
    division: str | None
    passages: List[str]


class PassageRecord(TypedDict):
    """One ``chat_retrieved_chunks`` record: a passage retrieved, kept or set aside."""
    position: int
    kept: bool
    drop_reason: str | None       # other_language or over_cap
    prompt_rank: int | None       # set once the prompt exists
    doc_number: int | None        # set once the prompt exists
    cited: bool | None            # set once the answer exists
    source: str
    hits: List[Hit]
    best_score: float | None
    chunk_id: str | None
    iddoc: str | None
    ref: str | None
    division: str | None
    url: str | None
    semantic_headers: str | None
    chunk_text: str | None


class VsFilters(TypedDict, total=False):
    """The Vector Search filter of a turn: the divisions searched (absent for no filter)."""
    division: List[str]


class RerankerSpec(TypedDict):
    """The reranker part of a Vector Search request."""
    model: str
    parameters: dict[str, List[str]]


class QueryPayload(TypedDict, total=False):
    """The body of a Vector Search query."""
    query_text: str
    columns: List[str]
    num_results: int
    query_type: str
    filters_json: str
    reranker: RerankerSpec


class RetrievalResult(TypedDict):
    """What ``retrieve_for_turn`` returns."""
    question: str
    fr_query: str
    en_query: str
    rows: List[SearchRow]
    named: List[str]
    index: str
    log: Any               # the TurnLog of the turn
    exact_citations: Dict[str, Any]   # "which documents cite X?": canonical code -> (code as written, citing documents)


class AnswerUsage(TypedDict, total=False):
    """Token counts and cost reported by the answer call."""
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    cost_eur: float


class AnswerFacts(TypedDict, total=False):
    """What the answer call reports about itself."""
    endpoint: str
    fallback: bool
    attempts: List[dict[str, Any]]
    queue_wait_ms: int
    truncated: bool
    continuations: int
    interrupted: bool
