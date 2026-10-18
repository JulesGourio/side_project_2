# Refonte du pipeline de parsing (démarrée le 2026-10-17)

Suivi pour Claude : constats, plan par phases, requêtes demandées et leurs résultats.

## Constats (run UAT `87f03f9a-b60f-40a7-a336-63561356d22a`, 2026-10-16)

- 2 339 fichiers re-parsés, dont 2 273 aux mêmes octets qu'en v1 mais à un autre chemin (`D_` au lieu de `Dm_`) : corrigé (reprise sur IDDOC + hash, plafond `parsing_max_parse_files`).
- Temps de parsing mesuré (`parse_time_seconds`) : 8,3 h au total pour ~80 h GPU de cluster (20 h x 4 GPU). Par type :
  docx Docling 1 052 fichiers 5,2 h (p50 12,6 s), pdf Docling 506 fichiers 2,4 h (p50 16 s), docx fallback XML 332 fichiers 0,3 h,
  pptx 158 fichiers 0,3 h, le reste (xls/xlsx/doc/antiword) ~0.
- Le checkpoint recevait ~95 fichiers toutes les ~30 min : ~75 s par fichier et par GPU contre ~13 s mesurées. L'écart n'est pas
  dans `parse_time_seconds` (sauvegarde des images, démarrage des workers Python, imports, rendu des pages) : le chronométrage par
  étape (`timings`, `total_seconds`) est activé depuis le 2026-10-17 pour le mesurer.
- docx et pptx n'utilisent aucun modèle GPU dans Docling (pipeline simple), ni antiword / openpyxl / xlrd / OLE : seuls les PDF
  (layout + TableFormer) ont besoin du GPU, soit ~22 % des fichiers de ce run.
- `patch_worker_env` fixe `OMP_NUM_THREADS=2` et Docling `num_threads=4` sur des nœuds de 16 vCPU (une tâche par nœud).
- Images : ~28 000 sauvegardées (PNG `optimize=True` + fsync, une par une).

## Mesures phase 0 (2026-10-17)

- Corpus (dernière ligne par IDDOC du manifest) : ~5 540 documents. PDF 1 449 (26 %, dont 24 en LLM-OCR), docx 3 086
  (Docling 1 441, fallback XML 1 638), pptx 324, xlsx/xlsm/xls/xlsb 457, doc 153, html 59, ppt 8 (5 encore `unsupported:ppt`
  de l'ancien parseur). Images : 60 700 dont 10 783 de PDF.
- Rythme : 17 à 79 nouveaux documents par semaine, 1 à 24 PDF par semaine, PDF 1 à 4 jours par semaine (120 jours).
- Images : DONE 38 679 (807 caractères en moyenne, 11,0 M tokens de sortie), SKIPPED 6 180, SKIPPED_DECORATIVE 4 499
  (sauvegardées pour rien, ~9 %), EXTRACTION_FAILED 12.
- Conséquence : un run quotidien a 0 à quelques PDF ; Docling PDF sur CPU suffit pour ce volume, le GPU ne sert qu'aux
  reconstructions complètes (> ~30 PDF).
- `uat_proj.qualibot.chunks_index` n'existe pas : le run du 16/10 s'est arrêté à GX dans `3_parse`, avant `5_sync_index`.

## Plan

- [x] Phase 0 — mesures : composition du corpus, rythme des nouveaux documents, devenir des images (2026-10-17).
- [ ] Phase 1 — GPU seulement pour les PDF (code poussé le 2026-10-17, à valider par un run UAT) : `3_parse` sur CPU
      (policy S, 16.4 standard, `r6id.xlarge`, Docling et torch par pip), PDF sur CPU jusqu'à 30, au-delà file
      `parsing_gpu_queue` et condition `3b_needs_gpu` -> `3b_parse_pdf` (GPU) ; `3c_chunks` (CPU) : retry, découpage, GX,
      écritures. Passage de relais : `parsing_run_scope`. `max_concurrent_runs: 1`. Points à surveiller au premier run :
      installation pip de Docling/torch sur 16.4, durée et mémoire de `3c_chunks` sur 4 vCPU / 32 Go (le policy S interdit
      plus que `*.xlarge`).
- [ ] Phase 2 — rendement du GPU sur les PDF : threads, 2 documents par GPU, partitions / réutilisation des workers,
      sauvegarde des images (PNG sans optimize, écritures en parallèle).
- [ ] Phase 3 — qualité : descriptions d'images rattachées par (IDDOC, image_id) — un re-parsing qui change l'ordre ou le
      nombre d'images attache une description à la mauvaise image : clé à passer sur le hash de l'image ; .doc / .ppt via LibreOffice vers docx / pptx puis Docling (tableaux, images), images décoratives
      non sauvegardées, versions Docling figées.
- [ ] Phase 4 — code : fusion des petits modules (`doc_status`, `parse_checks`, `image_store`, `tables`), `utils.py` renommé.

## Policy S (UAT, `000E7559CADE5290`, lue le 2026-10-17)

`spark_version` : `auto:latest-lts`, `auto:prev-lts`, `16.4.x-scala2.13`, `16.4.x-scala2.12` (aucun runtime ML) ;
`node_type_id` : `.*\.xlarge` ; `num_workers` max 0 ; SPOT_WITH_FALLBACK, `first_on_demand` >= 1 ; bibliothèques :
`great-expectations~=0.18.0`, `databricks-sdk>=0.20.0`, `fsspec~=2024.0`, `msal~=1.0`, `s3fs~=2024.0`,
`com.crealytics:spark-excel_2.12:3.5.0_0.20.3`, `xlrd~=2.0.0`, `openpyxl~=3.1.0`.
`system.lakeflow` / `system.billing` : pas de `USE SCHEMA` pour l'utilisateur (coût non mesurable par lui).

## Images (2026-10-17, avant le run de la phase 1)

- Lignes v1 du checkpoint des 2 318 documents re-parsés : `images` vide (0 image listée, `image_count` seul rempli),
  516 lignes du dossier `Dm_`. Images re-parsées : 27 974 ; descriptions v1 au même numéro : 8 069 même image
  (page + taille), 10 450 autre image (auraient été mal rattachées), 8 166 sans ligne v1.
- Corrigé : description gardée seulement pour la même image ; documents dont le checkpoint ne liste pas les images :
  lignes `image_metadata` gardées telles quelles ; la fusion incrémentale ne réapplique plus l'ancienne description.
- `4_describe_images` ne remet plus tout en PENDING en mode `full` : `parsing_redescribe_all_images` (false par défaut).

## Déploiement phase 1 (2026-10-17)

- 1er deploy refusé : dépendance de `3c_chunks` sur la condition sans `outcome` -> branche `false` + `3b_parse_pdf`,
  `run_if: AT_LEAST_ONE_SUCCESS`.
- 1er run : `3_parse` « Failure starting repl » sur la première cellule (`%pip opencv`) : Docling + torch en bibliothèques
  de cluster sur le runtime 16.4 standard cassent le démarrage du REPL. Passés en `%pip` dans les notebooks CPU
  (`create_proj_parsing_checkpoint.py`, `create_proj_chunks.py`) + `restartPython` ; plus de bibliothèque de cluster sur
  `3_parse` / `3c_chunks`. Le GPU (runtime ML) garde ses bibliothèques de tâche.

## Run phase 1, 2e essai (2026-10-18)

- `3_parse` : exception dans `parse_and_extract_images_udf` ligne 337 (`parse_with_docling`) : l'import de Docling sur les
  workers (seul appel hors `try`). Ajouté : `parse_steps.check_docling_on_workers()` dans `setup_workers` (import sur un
  exécuteur avant tout scan, traceback complet dans l'erreur). Cause exacte à lire au prochain run.
- Embeddings réutilisables : `embedding_writeback_table=<index>_embeddings` à la création d'un index par
  `update_proj_vector_indexes` (l'index UAT `chunks_index` n'existe pas encore : sera créé avec). Pour DEV : index à
  embeddings fournis (`embedding_vector_column`) depuis une copie de `chunks_index_embeddings`, et l'app doit alors envoyer
  `query_vector` (appel de l'endpoint d'embedding) en plus de `query_text` — à faire après validation UAT.
- Cause probable trouvée : `opencv-python-headless==4.12.0.88` exige numpy >= 2, les notebooks CPU installent numpy < 2 (runtime 16.4) et `--no-deps` ne le voit pas -> 4.11.0.86 sur les tâches CPU (antérieure à 4.13, qui plante sur l'OpenSSL du runtime). GPU inchangé (4.12, validé).
- 3e essai (2026-10-18) : la sonde montre `AttributeError: force_full_page_ocr` dans `docling.datamodel.pipeline_options` : pydantic du runtime 16.4 (`/databricks/python`) trop ancien, pip ne l'a pas réinstallé dans l'env du notebook. Reproduit en local avec pydantic 2.8.2 ; OK avec 2.10.6. Épinglés : `docling==2.140.0`, `docling-core==2.102.0`, `pydantic>=2.10.3,<3`, `pandas>=2.1.4,<3` (docling-core exige pandas >= 2.1.4, Spark du runtime : pandas < 3), `numpy<2` ; conversion docx testée en local (Python 3.12).
- 4e essai (2026-10-18) : `3_parse` OK sur CPU (41 fichiers, 277 s de parsing, 0 échec) mais 26 min jusqu'au bilan : `log_run_summary` / `show_failed_documents` triaient tout le checkpoint (texte compris) -> bilan limité aux lignes du run, dédoublonnage sur les seules colonnes utiles.
- `4_describe_images` : `ValueError: MERGE INTO must have at least one MERGE clause` au premier enregistrement (après ~2 000 appels LLM, perdus) : `leap_utils.merge_into` refuse un MERGE « WHEN MATCHED » seul sans `matched_clause`. Contourné dans `tables.merge_into` (source réduite aux clés existantes, clause INSERT inoffensive), `leap_utils` non modifié.
