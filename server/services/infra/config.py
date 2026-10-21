"""Settings of the shared services: Vector Search queries and the impact search judge."""

# One Vector Search query (chat and impact search).
VECTOR_SEARCH_QUERY_TIMEOUT_S = 30.0

# Impact search judge: one LLM call per candidate document.
IMPACT_JUDGE_TIMEOUT_S = 90.0
# Cost scales with max_candidates * this * IMPACT_PASSAGE_CHARS; lower-scoring hits past this cap are the least likely
# to hold the conflict.
IMPACT_PASSAGES_PER_CANDIDATE = 10
IMPACT_PASSAGE_CHARS = 3000
# The embedding endpoint behind query_text rejects concurrent requests with "Request id already running" under load:
# the fan-out stays narrow and a rejected query is retried.
IMPACT_QUERY_CONCURRENCY = 3
IMPACT_QUERY_RETRIES = 2
IMPACT_JUDGE_CONCURRENCY = 6
IMPACT_JUDGE_RETRIES = 1
# A reasoning judge spends max_tokens on hidden reasoning and can return an empty answer (finish_reason 'length') on a
# candidate with many passages; the retry gets a larger budget, capped.
IMPACT_JUDGE_TRUNCATION_BUDGET_FACTOR = 3
IMPACT_JUDGE_MAX_TOKENS_CAP = 8000
