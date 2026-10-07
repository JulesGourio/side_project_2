-- Golden evaluation — compare attempts (eval ids) in dev_landingzone.qualibot.eval_golden_runs
-- Run the whole script in the SQL editor (DEV warehouse). Parameters (fields above the editor):
--   :eval_a / :eval_b  two eval ids to compare head to head, e.g. ka / baseline
--   :question_like     part of a question, to read the answers side by side, e.g. NDT
-- Scores: correctness / guidelines = 1 (yes) or 0 (no), doc_recall = 0..1 (NULL when the case
-- expects no document), latency in seconds. "Latest" = the most recent attempt of an eval id.

CREATE OR REPLACE TEMPORARY VIEW latest AS
SELECT r.*
FROM dev_landingzone.qualibot.eval_golden_runs r
JOIN (SELECT eval_id, max(attempt_ts) AS ts FROM dev_landingzone.qualibot.eval_golden_runs GROUP BY eval_id) l
  ON r.eval_id = l.eval_id AND r.attempt_ts = l.ts;

-- 1. Leaderboard — latest attempt of each eval id
SELECT eval_id, engine, coalesce(vsi_llm_endpoint, ka_endpoint) AS model_or_endpoint,
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
GROUP BY ALL
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

-- 3. Question × eval id grid (latest attempts): correctness and doc recall per eval id
SELECT question, case_kind,
       map_from_entries(collect_list(struct(eval_id, correctness))) AS correct_by_eval,
       map_from_entries(collect_list(struct(eval_id, doc_recall)))  AS doc_recall_by_eval,
       round(avg(correctness) * 100) AS correct_pct_all_evals
FROM latest
GROUP BY question, case_kind
ORDER BY correct_pct_all_evals, question;

-- 4. Head to head :eval_a vs :eval_b, question by question (latest attempts)
SELECT a.question, a.case_kind,
       a.correctness AS a_correct, b.correctness AS b_correct,
       a.guidelines  AS a_guide,   b.guidelines  AS b_guide,
       a.doc_recall  AS a_recall,  b.doc_recall  AS b_recall,
       a.latency_s   AS a_s,       b.latency_s   AS b_s,
       CASE WHEN b.correctness > a.correctness THEN concat(:eval_b, ' better')
            WHEN b.correctness < a.correctness THEN concat(:eval_a, ' better')
            WHEN coalesce(b.doc_recall, 0) > coalesce(a.doc_recall, 0) THEN concat(:eval_b, ' more docs')
            WHEN coalesce(b.doc_recall, 0) < coalesce(a.doc_recall, 0) THEN concat(:eval_a, ' more docs')
            ELSE 'tie' END AS verdict,
       array_except(a.golden_refs, a.answer_refs) AS a_missed_refs,
       array_except(b.golden_refs, b.answer_refs) AS b_missed_refs
FROM latest a
JOIN latest b ON a.dataset_record_id = b.dataset_record_id
WHERE a.eval_id = :eval_a AND b.eval_id = :eval_b
ORDER BY verdict, a.question;

-- 5. Head to head :eval_a vs :eval_b — score card
SELECT sum(CASE WHEN b.correctness > a.correctness THEN 1 ELSE 0 END) AS b_wins,
       sum(CASE WHEN b.correctness < a.correctness THEN 1 ELSE 0 END) AS a_wins,
       sum(CASE WHEN b.correctness = a.correctness THEN 1 ELSE 0 END) AS ties,
       round(avg(b.doc_recall - a.doc_recall) * 100, 1)                AS doc_recall_gain_pts,
       round(avg(b.latency_s - a.latency_s), 1)                        AS latency_delta_s
FROM latest a
JOIN latest b ON a.dataset_record_id = b.dataset_record_id
WHERE a.eval_id = :eval_a AND b.eval_id = :eval_b;

-- 6. Expected documents each eval id missed (latest attempts)
SELECT eval_id, question, golden_refs,
       array_except(golden_refs, answer_refs) AS missed_refs,
       array_except(answer_refs, golden_refs) AS extra_refs
FROM latest
WHERE size(golden_refs) > 0 AND size(array_except(golden_refs, answer_refs)) > 0
ORDER BY question, eval_id;

-- 7. By kind of case, question language and conversation length (latest attempts)
SELECT eval_id, case_kind,
       CASE WHEN question_lang = '' THEN 'fr/en' ELSE question_lang END AS lang,
       CASE WHEN turns > 1 THEN 'follow-up' ELSE 'single question' END AS conversation,
       count(*)                         AS cases,
       round(avg(correctness) * 100, 1) AS correct_pct,
       round(avg(doc_recall) * 100, 1)  AS doc_recall_pct
FROM latest
GROUP BY ALL
ORDER BY case_kind, lang, conversation, eval_id;

-- 8. Unstable questions: same eval id, the verdict changes from one attempt to another
SELECT eval_id, question, count(*) AS attempts,
       round(avg(correctness) * 100) AS correct_pct_over_attempts,
       round(stddev(doc_recall), 2)  AS doc_recall_stddev
FROM dev_landingzone.qualibot.eval_golden_runs
GROUP BY eval_id, question
HAVING count(*) > 1 AND (min(correctness) <> max(correctness) OR stddev(doc_recall) > 0)
ORDER BY eval_id, correct_pct_over_attempts;

-- 9. Answers side by side for one question (latest attempts)
SELECT eval_id, correctness, guidelines, doc_recall, answer_refs, answer
FROM latest
WHERE question ILIKE concat('%', :question_like, '%')
ORDER BY eval_id;
