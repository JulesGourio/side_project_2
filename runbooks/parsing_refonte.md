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

- [ ] Phase 0 — mesures : composition du corpus, rythme des nouveaux documents, devenir des images.
- [ ] Phase 1 — GPU seulement pour les PDF : tâche CPU (sélection, hash, reprise, formats non PDF, docx/pptx Docling CPU) ;
      tâche GPU lancée seulement s'il reste des PDF (condition sur une valeur de tâche) ; découpage, contrôles GX et écritures
      sur CPU après.
- [ ] Phase 2 — rendement du GPU sur les PDF : threads, 2 documents par GPU, partitions / réutilisation des workers,
      sauvegarde des images (PNG sans optimize, écritures en parallèle).
- [ ] Phase 3 — qualité : .doc / .ppt via LibreOffice vers docx / pptx puis Docling (tableaux, images), images décoratives
      non sauvegardées, versions Docling figées.
- [ ] Phase 4 — code : fusion des petits modules (`doc_status`, `parse_checks`, `image_store`, `tables`), `utils.py` renommé.
