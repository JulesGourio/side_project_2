-- Golden evaluation — SQL comparisons over the results written by golden_eval_ka_vs_vsi.py
-- Table: dev_landingzone.qualibot.eval_golden_results (one row per case × engine × run).
-- Scores: correctness / guidelines = 1 (yes) or 0 (no), doc_recall = 0..1 (NULL when the case
-- expects no document), latency_s / first_token_s in seconds.
-- Paste the queries one at a time in a SQL editor or in %sql cells.

-- 1. Summary per run and engine ---------------------------------------------------------
SELECT run_ts, run_tag, engine, coalesce(vsi_llm_endpoint, ka_endpoint) AS model_or_endpoint,
       count(*)                                 AS cases,
       round(avg(correctness) * 100, 1)         AS correctness_pct,
       round(avg(guidelines) * 100, 1)          AS guidelines_pct,
       round(avg(doc_recall) * 100, 1)          AS doc_recall_pct,
       round(percentile(latency_s, 0.5), 1)     AS latency_p50_s,
       round(percentile(latency_s, 0.9), 1)     AS latency_p90_s,
       round(percentile(first_token_s, 0.5), 1) AS first_token_p50_s
FROM dev_landingzone.qualibot.eval_golden_results
GROUP BY ALL
ORDER BY run_ts DESC, engine;

-- 2. Average over every run, per engine/model (smooths run-to-run variance) ---------------
SELECT engine, coalesce(vsi_llm_endpoint, ka_endpoint) AS model_or_endpoint,
       count(DISTINCT run_ts)                   AS runs,
       round(avg(correctness) * 100, 1)         AS correctness_pct,
       round(avg(guidelines) * 100, 1)          AS guidelines_pct,
       round(avg(doc_recall) * 100, 1)          AS doc_recall_pct,
       round(avg(latency_s), 1)                 AS latency_avg_s
FROM dev_landingzone.qualibot.eval_golden_results
GROUP BY ALL
ORDER BY engine, model_or_endpoint;

-- 3. Latest notebook run: KA vs VSI side by side, case by case -----------------------------
WITH last AS (SELECT max(run_ts) AS ts FROM dev_landingzone.qualibot.eval_golden_results
              WHERE run_ts IN (SELECT run_ts FROM dev_landingzone.qualibot.eval_golden_results
                               GROUP BY run_ts HAVING count(DISTINCT engine) = 2)),
r AS (SELECT * FROM dev_landingzone.qualibot.eval_golden_results WHERE run_ts = (SELECT ts FROM last))
SELECT k.question, k.case_kind,
       k.correctness AS ka_correct, v.correctness AS vsi_correct,
       k.guidelines  AS ka_guide,   v.guidelines  AS vsi_guide,
       k.doc_recall  AS ka_recall,  v.doc_recall  AS vsi_recall,
       k.latency_s   AS ka_s,       v.latency_s   AS vsi_s,
       CASE WHEN v.correctness > k.correctness THEN 'VSI better'
            WHEN v.correctness < k.correctness THEN 'KA better'
            WHEN coalesce(v.doc_recall, 0) > coalesce(k.doc_recall, 0) THEN 'VSI more docs'
            WHEN coalesce(v.doc_recall, 0) < coalesce(k.doc_recall, 0) THEN 'KA more docs'
            ELSE 'tie' END AS verdict
FROM r k JOIN r v ON k.dataset_record_id = v.dataset_record_id AND k.engine = 'ka' AND v.engine = 'vsi'
ORDER BY verdict, k.question;

-- 4. Win / loss / tie count on correctness, per notebook run ------------------------------
SELECT k.run_ts, k.run_tag,
       sum(CASE WHEN v.correctness > k.correctness THEN 1 ELSE 0 END) AS vsi_wins,
       sum(CASE WHEN v.correctness < k.correctness THEN 1 ELSE 0 END) AS ka_wins,
       sum(CASE WHEN v.correctness = k.correctness THEN 1 ELSE 0 END) AS ties
FROM dev_landingzone.qualibot.eval_golden_results k
JOIN dev_landingzone.qualibot.eval_golden_results v
  ON k.run_ts = v.run_ts AND k.dataset_record_id = v.dataset_record_id AND k.engine = 'ka' AND v.engine = 'vsi'
GROUP BY ALL
ORDER BY k.run_ts DESC;

-- 5. Expected documents each engine missed (latest run of each engine) --------------------
WITH last AS (SELECT engine, max(run_ts) AS ts FROM dev_landingzone.qualibot.eval_golden_results GROUP BY engine)
SELECT r.engine, r.question, r.golden_refs,
       array_except(r.golden_refs, r.answer_refs) AS missed_refs,
       array_except(r.answer_refs, r.golden_refs) AS extra_refs
FROM dev_landingzone.qualibot.eval_golden_results r JOIN last l ON r.engine = l.engine AND r.run_ts = l.ts
WHERE size(r.golden_refs) > 0 AND size(array_except(r.golden_refs, r.answer_refs)) > 0
ORDER BY r.question, r.engine;

-- 6. Unstable cases: same engine, verdict changes from one run to another -----------------
SELECT engine, coalesce(vsi_llm_endpoint, ka_endpoint) AS model_or_endpoint, question,
       count(*)                          AS runs,
       round(avg(correctness) * 100)     AS correct_pct_over_runs,
       round(stddev(doc_recall), 2)      AS recall_stddev
FROM dev_landingzone.qualibot.eval_golden_results
GROUP BY ALL
HAVING count(*) > 1 AND (min(correctness) <> max(correctness) OR stddev(doc_recall) > 0)
ORDER BY engine, correct_pct_over_runs;

-- 7. By kind of case: factual questions vs expected refusals / "not in the docs" ----------
SELECT engine, case_kind, count(*) AS cases,
       round(avg(correctness) * 100, 1) AS correctness_pct,
       round(avg(guidelines) * 100, 1)  AS guidelines_pct
FROM dev_landingzone.qualibot.eval_golden_results
GROUP BY ALL
ORDER BY case_kind, engine;

-- 8. By question language (translation bridge used for es / cs …) and by conversation length
SELECT engine, CASE WHEN question_lang = '' THEN 'fr/en (no bridge)' ELSE question_lang END AS lang,
       CASE WHEN turns > 1 THEN 'follow-up' ELSE 'single question' END AS conversation,
       count(*) AS cases,
       round(avg(correctness) * 100, 1) AS correctness_pct,
       round(avg(doc_recall) * 100, 1)  AS doc_recall_pct
FROM dev_landingzone.qualibot.eval_golden_results
GROUP BY ALL
ORDER BY lang, conversation, engine;

-- 9. VSI models compared (runs made with different vsi_llm_endpoint values) ---------------
SELECT vsi_llm_endpoint, count(DISTINCT run_ts) AS runs,
       round(avg(correctness) * 100, 1)     AS correctness_pct,
       round(avg(guidelines) * 100, 1)      AS guidelines_pct,
       round(avg(doc_recall) * 100, 1)      AS doc_recall_pct,
       round(percentile(latency_s, 0.5), 1) AS latency_p50_s,
       round(percentile(first_token_s, 0.5), 1) AS first_token_p50_s
FROM dev_landingzone.qualibot.eval_golden_results
WHERE engine = 'vsi'
GROUP BY ALL
ORDER BY correctness_pct DESC;

-- 10. Read both answers to one question (edit the LIKE pattern) ----------------------------
SELECT run_ts, run_tag, engine, correctness, guidelines, doc_recall, answer_refs, answer
FROM dev_landingzone.qualibot.eval_golden_results
WHERE question LIKE '%NDT/NDI%'
ORDER BY run_ts DESC, engine;
