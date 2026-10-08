-- Golden evaluation — compare attempts (eval ids) in dev_landingzone.qualibot.eval_golden_runs
-- Run the whole script in the SQL editor (DEV warehouse). No parameter to fill:
-- the reference attempt is set once below (view `ref`, default 'ka'), every other eval id
-- is compared to it. Question to read side by side: query 10, edit the LIKE text.
-- correctness / guidelines = 1 (yes) or 0 (no), doc_recall = 0..1 (NULL when the case
-- expects no document), latency in seconds. "Latest" = most recent attempt of an eval id.

CREATE OR REPLACE TEMPORARY VIEW ref AS SELECT 'ka' AS eval_id;   -- reference attempt

CREATE OR REPLACE TEMPORARY VIEW latest AS
SELECT r.*, coalesce(r.vsi_variant, CASE WHEN r.engine = 'vsi' THEN 'baseline' END) AS variant
FROM dev_landingzone.qualibot.eval_golden_runs r
JOIN (SELECT eval_id, max(attempt_ts) AS ts FROM dev_landingzone.qualibot.eval_golden_runs GROUP BY eval_id) l
  ON r.eval_id = l.eval_id AND r.attempt_ts = l.ts;

-- 1. Leaderboard — latest attempt of each eval id, with what it ran
SELECT eval_id, engine, variant, coalesce(vsi_llm_endpoint, ka_endpoint) AS model_or_endpoint,
       max(vsi_settings)                        AS settings,
       max(app_code_hash)                       AS code,
       max(attempt_ts)                          AS attempt_ts,
       max(notes)                               AS notes,
       count(*)                                 AS cases,
       round(avg(correctness) * 100, 1)         AS correct_pct,
       round(avg(guidelines) * 100, 1)          AS guidelines_pct,
       round(avg(doc_recall) * 100, 1)          AS doc_recall_pct,
       round(percentile(latency_s, 0.5), 1)     AS latency_p50_s,
       round(percentile(latency_s, 0.9), 1)     AS latency_p90_s,
       round(percentile(first_token_s, 0.5), 1) AS first_token_p50_s
FROM latest
GROUP BY eval_id, engine, variant, coalesce(vsi_llm_endpoint, ka_endpoint)
ORDER BY correct_pct DESC, doc_recall_pct DESC;

-- 2. Every attempt of every eval id, with the spread between attempts
WITH per_attempt AS (
  SELECT eval_id, attempt_ts, avg(correctness) AS c, avg(guidelines) AS g, avg(doc_recall) AS d, avg(latency_s) AS l
  FROM dev_landingzone.qualibot.eval_golden_runs GROUP BY eval_id, attempt_ts)
SELECT eval_id, count(*) AS attempts,
       round(avg(c) * 100, 1)    AS correct_pct_avg,
       round(min(c) * 100, 1)    AS correct_pct_min,
       round(max(c) * 100, 1)    AS correct_pct_max,
       round(avg(g) * 100, 1)    AS guidelines_pct_avg,
       round(avg(d) * 100, 1)    AS doc_recall_pct_avg,
       round(stddev(d) * 100, 1) AS doc_recall_pct_stddev,
       round(avg(l), 1)          AS latency_avg_s
FROM per_attempt
GROUP BY eval_id
ORDER BY correct_pct_avg DESC;

-- 3. Question × eval id grid (latest attempts)
SELECT question, case_kind,
       map_from_entries(collect_list(struct(eval_id, correctness))) AS correct_by_eval,
       map_from_entries(collect_list(struct(eval_id, doc_recall)))  AS doc_recall_by_eval,
       round(avg(correctness) * 100) AS correct_pct_all_evals
FROM latest
GROUP BY question, case_kind
ORDER BY correct_pct_all_evals, question;

-- 4. Every eval id vs the reference, question by question (latest attempts)
SELECT b.eval_id, a.question, a.case_kind,
       a.correctness AS ref_correct, b.correctness AS correct,
       a.doc_recall  AS ref_recall,  b.doc_recall  AS recall,
       a.latency_s   AS ref_s,       b.latency_s   AS s,
       CASE WHEN b.correctness > a.correctness THEN 'better'
            WHEN b.correctness < a.correctness THEN 'worse'
            WHEN coalesce(b.doc_recall, 0) > coalesce(a.doc_recall, 0) THEN 'same, more docs'
            WHEN coalesce(b.doc_recall, 0) < coalesce(a.doc_recall, 0) THEN 'same, fewer docs'
            ELSE 'same' END AS vs_ref,
       array_except(a.golden_refs, a.answer_refs) AS ref_missed_refs,
       array_except(b.golden_refs, b.answer_refs) AS missed_refs
FROM latest a
JOIN latest b ON a.question = b.question
WHERE a.eval_id = (SELECT eval_id FROM ref) AND b.eval_id <> a.eval_id
ORDER BY b.eval_id, vs_ref, a.question;

-- 5. Every eval id vs the reference — score card
SELECT b.eval_id,
       sum(CASE WHEN b.correctness > a.correctness THEN 1 ELSE 0 END) AS better,
       sum(CASE WHEN b.correctness < a.correctness THEN 1 ELSE 0 END) AS worse,
       sum(CASE WHEN b.correctness = a.correctness THEN 1 ELSE 0 END) AS same,
       round(avg(b.correctness - a.correctness) * 100, 1)              AS correct_gain_pts,
       round(avg(b.doc_recall - a.doc_recall) * 100, 1)                AS doc_recall_gain_pts,
       round(avg(b.latency_s - a.latency_s), 1)                        AS latency_delta_s
FROM latest a
JOIN latest b ON a.question = b.question
WHERE a.eval_id = (SELECT eval_id FROM ref) AND b.eval_id <> a.eval_id
GROUP BY b.eval_id
ORDER BY correct_gain_pts DESC;

-- 6. Expected documents missed — and why (VSI attempts saved with retrieved_refs):
--    not_retrieved = search never found it (retrieval gap), retrieved_not_cited = the LLM had it
SELECT eval_id, question, golden_refs,
       array_except(golden_refs, answer_refs)                                    AS missed_refs,
       CASE WHEN size(retrieved_refs) > 0
            THEN array_except(array_except(golden_refs, answer_refs), retrieved_refs) END AS not_retrieved,
       CASE WHEN size(retrieved_refs) > 0
            THEN array_intersect(array_except(golden_refs, answer_refs), retrieved_refs) END AS retrieved_not_cited,
       array_except(answer_refs, golden_refs)                                    AS extra_refs
FROM latest
WHERE size(golden_refs) > 0 AND size(array_except(golden_refs, answer_refs)) > 0
ORDER BY question, eval_id;

-- 7. Retrieval vs generation gaps per VSI eval id (needs retrieved_refs)
SELECT eval_id,
       sum(size(golden_refs))                                                   AS expected_docs,
       sum(size(array_intersect(golden_refs, retrieved_refs)))                  AS retrieved,
       sum(size(array_intersect(golden_refs, answer_refs)))                     AS cited,
       round(sum(size(array_intersect(golden_refs, retrieved_refs))) * 100 / sum(size(golden_refs)), 1) AS retrieval_recall_pct,
       round(sum(size(array_intersect(golden_refs, answer_refs))) * 100 / sum(size(golden_refs)), 1)    AS citation_recall_pct
FROM latest
WHERE engine = 'vsi' AND size(retrieved_refs) > 0 AND size(golden_refs) > 0
GROUP BY eval_id
ORDER BY retrieval_recall_pct DESC;

-- 8. By kind of case, question language and conversation length (latest attempts)
SELECT eval_id, case_kind,
       CASE WHEN question_lang IN ('', 'fr', 'en') OR question_lang IS NULL THEN 'fr/en' ELSE question_lang END AS lang,
       CASE WHEN turns > 1 THEN 'follow-up' ELSE 'single question' END AS conversation,
       count(*)                         AS cases,
       round(avg(correctness) * 100, 1) AS correct_pct,
       round(avg(doc_recall) * 100, 1)  AS doc_recall_pct
FROM latest
GROUP BY ALL
ORDER BY case_kind, lang, conversation, eval_id;

-- 9. Unstable questions: same eval id, the verdict changes from one attempt to another
SELECT eval_id, question, count(*) AS attempts,
       round(avg(correctness) * 100) AS correct_pct_over_attempts,
       round(stddev(doc_recall), 2)  AS doc_recall_stddev
FROM dev_landingzone.qualibot.eval_golden_runs
GROUP BY eval_id, question
HAVING count(*) > 1 AND (min(correctness) <> max(correctness) OR stddev(doc_recall) > 0)
ORDER BY eval_id, correct_pct_over_attempts;

-- 10. Answers side by side for one question (edit the LIKE text)
SELECT eval_id, correctness, guidelines, doc_recall, answer_refs, retrieved_refs, answer
FROM latest
WHERE question ILIKE '%template du CMP%'
ORDER BY eval_id;
