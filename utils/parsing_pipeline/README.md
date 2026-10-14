# Pipeline de parsing

Chaîne quotidienne qui transforme les documents de la GED Intraqual en passages
indexés. Le chat et la recherche d'impact lisent ces passages.

La chaîne tourne sur Databricks, en six tâches enchaînées, définies dans
[`resources/parsing_pipeline.job.yml`](../../resources/parsing_pipeline.job.yml).
Le job s'appelle `D_1_Qualibot_Parsing_Pipeline_<cible>` et se déclenche chaque nuit
à 4 h (Europe/Paris). La planification est réglée par la variable
`parsing_schedule_pause` (`PAUSED` par défaut dans `databricks.yml` ; la cible
`qualibot-uat` la met à `UNPAUSED`).

Les notebooks suivent la structure LEAP (en-tête, `Technical debt`,
`Configuration`, `Inputs`, `Data Preparation`, `Data Transformations`,
`Quality Checks`, `Outputs`). La logique est dans les modules à côté d'eux : le
notebook ne fait qu'orchestrer.

## Fichiers

| Fichier | Rôle |
|---|---|
| `create_proj_category_reference.py` | Tâche `1_categories` : garde de fraîcheur des sources, table `category_reference` |
| `create_proj_parse_manifest.py` | Tâche `2_manifest` : périmètre, nettoyage, classement des dossiers, table `parse_manifest` |
| `create_proj_chunks.py` | Tâche `3_parse` : sélection, parsing, reprise, découpage, écriture des tables |
| `create_proj_image_metadata.py` | Tâche `4_describe_images` : description des images par le modèle de vision |
| `update_proj_vector_indexes.py` | Tâche `5_sync_index` : création des index absents, synchronisation, `chunks_full` |
| `create_proj_doc_catalog.py` | Tâche `6_update_kb_metadata` : catalogue des documents et date de la base dans Lakebase |
| `config.py` | Tous les paramètres (variables `PARSING_*`, valeurs par défaut), les seuils et les prompts des modèles |
| `categories.py` | Hiérarchie des catégories, division, périmètre Intraqual (Spark) |
| `manifest_steps.py` | Phases de `2_manifest` : nettoyage des contenus obsolètes, classement des dossiers, manifeste |
| `selection.py` | Choix du meilleur fichier par IDDOC (ordre des formats) |
| `parse_steps.py` | Phases de `3_parse` : périmètre du run, parsing par lots, reprise, relance des échecs |
| `build_steps.py` | Construction des tables de `3_parse` : `image_metadata`, `processed_files`, passages, notices d'archive |
| `output_steps.py` | Écritures de `3_parse` : tables, historique du run, notices, contrôle des documents sans passage |
| `describe_steps.py` | Phases de `4_describe_images` : appels au modèle, passages d'images, promotion des documents |
| `llm_describe.py` | Appels asynchrones au modèle de vision : limiteur de débit, chaîne d'endpoints, disjoncteur |
| `format_parsers.py` | Octets vers texte, un parseur par format (Docling et secours) |
| `image_utils.py` | UDF de parsing de `3_parse` : Docling ou secours, extraction des images, sauvegarde |
| `image_fallbacks.py` | Texte et images des fichiers que Docling ne lit pas : DOCX/PPTX (XML), `.ppt` (OLE), pages de PDF scannés |
| `image_store.py` | Écriture d'une image sur le volume, avec contrôle du chemin |
| `chunking.py` | Découpage d'un Markdown en passages (sections, tables, préfixe de provenance) |
| `doc_status.py` | Statuts partagés des documents et des images |
| `quality.py` | Contrôles de qualité RED et AMBER sur les tables |
| `parse_checks.py` | Contrôle de cohérence des REF entre passages et `parse_manifest` (AMBER) |
| `tables.py` | Seule couche d'écriture Delta (`save_table`, `merge_into`, via `leap_utils`) |
| `utils.py` | Runtime partagé : configuration des workers, comptage de tokens, helpers Spark |

Les modules `chunking.py`, `quality.py`, `doc_status.py` et `tables.py` sont
testés en unitaire (`tests/test_parsing_*.py`). Les modules listés dans
`config.WORKER_MODULES` sont envoyés aux workers Spark ; les autres restent côté
driver.

## Lancer

- La chaîne tourne dans le job. Déployer le code et le job :
  `databricks bundle deploy -t <cible>`, puis lancer une exécution :
  `databricks bundle run parsing_pipeline -t <cible>`.
- Le code des notebooks et des modules est synchronisé avec le bundle. L'option
  `-Infra` de [`deploy/deploy_qualibot.ps1`](../deploy/deploy_qualibot.ps1) relance
  le déploiement complet du bundle pour la cible ; sans elle, `-SyncOnly` ne
  synchronise que le code.
- Une exécution ciblée passe la liste d'IDDOC dans la variable
  `parsing_parse_filter` (`databricks bundle deploy -t <cible> --var parsing_parse_filter=123,456`
  puis lancement du job).
- Les étapes à faire dans Databricks (copie des tables, premières exécutions,
  activation de la planification) sont dans [runbooks/uat.md](../../runbooks/uat.md)
  et [runbooks/dev.md](../../runbooks/dev.md). Ce README ne les répète pas.
- Tests unitaires :

```
python -m pytest tests/test_parsing_*.py tests/test_code_conventions.py
```

## Les six tâches

| Tâche | Notebook | Cluster | Rôle | Écrit |
|---|---|---|---|---|
| `1_categories` | `create_proj_category_reference.py` | CPU (`meta_cpu_cluster`) | Vérifie la fraîcheur des sources Intraqual, construit la hiérarchie des catégories et les divisions | `category_reference` |
| `2_manifest` | `create_proj_parse_manifest.py` | CPU (`meta_cpu_cluster`) | Applique le périmètre, retire les contenus obsolètes, classe les dossiers du volume, construit la liste des documents du jour | `parse_manifest`, `processed_files`, et suppressions dans les tables de passages |
| `3_parse` | `create_proj_chunks.py` | GPU (`parse_gpu_cluster`) | Choisit un fichier par document, le parse, extrait les images, découpe en passages et écrit les tables | `_pipeline_checkpoint`, `processed_files`, `chunks`, `chunks_archive`, `chunks_archive_notices`, `image_metadata`, `parsing_run_health`, `document_change_log` |
| `4_describe_images` | `create_proj_image_metadata.py` | CPU (`describe_cpu_cluster`) | Décrit les images par un modèle de vision et ajoute leurs passages | `image_metadata`, `chunks`, `chunks_archive`, `processed_files` |
| `5_sync_index` | `update_proj_vector_indexes.py` | serverless | Crée les index absents, lance leur synchronisation, reconstruit `chunks_full` si demandé | `chunks_full`, index Vector Search |
| `6_update_kb_metadata` | `create_proj_doc_catalog.py` | serverless | Reconstruit le catalogue des documents et la date de la base dans Lakebase | `doc_catalog`, `knowledge_base_metadata` (Lakebase) |

Chaque tâche attend la tâche qui la précède. Si `5_sync_index` échoue, le catalogue
n'est pas mis à jour. Sans la tâche 5, les nouveaux passages ne sont jamais
interrogeables : les index sont en mode `TRIGGERED`.

**Sources** :

- Métadonnées : les vues `gd_doc_latest`, `gd_doc_cat_latest`, `gd_cat_latest`,
  `gd_typdoc_latest` et `gd_utilisateur_latest` du schéma
  `prod_bronze.intraqual` (variable `intraqual_bronze_catalog_schema`).
- Fichiers : le volume source Intraqual `/Volumes/prod_landingzone/intraqual/intraqual_documents`
  (variable `intraqual_volume_root`), un dossier `D_<IDDOC>` (ou `Dm_<IDDOC>`) par
  document.

**Garde de fraîcheur** : `1_categories` échoue si une source a plus de
`PARSING_MAX_SOURCE_STALENESS_HOURS` heures (36 par défaut ; 0 désactive la garde).
Une chaîne qui tourne sur des données périmées s'arrête donc au lieu d'indexer un
corpus partiel.

## Périmètre

- **Périmètre** (`DOC_SCOPE_FILTER`, `config.py`) : `courant = 1 AND etat = 7 AND
  nonvisible = 0` sur `gd_doc_latest`. Seule la révision courante d'un document,
  à l'état 7 et non masqué, entre dans la chaîne. `difftotale` n'est pas un
  critère : des documents affichés par le portail ont cette valeur à 0.
- **Une révision, un IDDOC** : chaque révision d'un document a son propre IDDOC.
  Quand un IDDOC sort du périmètre (révision remplacée ou document supprimé),
  `2_manifest` supprime ses lignes dans `chunks`, `chunks_archive`,
  `image_metadata` et `processed_files`. Quand un REF a encore plusieurs IDDOC
  dans le périmètre, seule la révision la plus récente (`indice` le plus haut)
  garde ses passages. Les lignes `processed_files` des autres restent, pour qu'elles
  ne soient pas parsées à nouveau.
- **Suppressions** : cette étape s'exécute à chaque run. Le paramètre
  `DRY_RUN` ne protège que les fichiers du volume (ceux des dossiers hors
  périmètre, supprimés seulement si `DRY_RUN` vaut `non`). Il ne protège pas les
  lignes des tables.
- **Classement des dossiers du volume** : `KEEP` (à parser), `DELETE` (hors
  périmètre), `ORPHAN` (IDDOC absent de `gd_doc`, conservé sur le volume mais non
  parsé), `SKIP` (nom de dossier non conforme), `MANUAL` (REF exclu à la main,
  `MANUAL_REF_EXCLUSIONS`), `CAT_EXCL` (catégorie exclue, `EXCLUDED_IDCATS`).
- **Documents antérieurs à 2018** (`DOC_DATE_CUTOFF`, `2018-01-01`) : ils passent par la même
  chaîne, mais leurs passages vont dans `chunks_archive`, jamais dans `chunks`
  (l'index du chat). `PARSING_ARCHIVE_MAX_DOCS` fixe le nombre total de ces
  documents parsés, les plus récents d'abord (0 : aucun, -1 : tous). Un
  passage-notice sans contenu par document antérieur est écrit dans
  `chunks_archive_notices` ; il n'entre dans `chunks` que si
  `PARSING_ARCHIVE_NOTICES_IN_RAG` vaut `true`.
- **Statuts de `processed_files`** (une ligne par IDDOC) :

| `parse_status` | Sens |
|---|---|
| `SUCCESS` | Parsé, passages écrits |
| `ERROR`, `TIMEOUT` | Échec du parsing ou dépassement de `PARSE_TIMEOUT_SECONDS` (200 s) : retenté au run suivant |
| `EMPTY_TEXT` | Aucun texte et aucune image restant à décrire |
| `IMAGES_PENDING` | Aucun texte, des images restent à décrire : le parsing ne le reprend pas, `4_describe_images` le fait passer en `SUCCESS` ou `SKIPPED_EMPTY_IMAGES` |
| `SKIP_TOO_LARGE` | Au-delà du plafond de taille du format |
| `SKIPPED_EMPTY_SMALL_FILE` | Fichier vide de moins de 50 Kio : gabarit sans contenu, exclu |
| `SKIPPED_EMPTY_IMAGES` | Toutes les images décrites comme non informatives |
| `SKIPPED_EMPTY_FOLDER` | Dossier sans fichier exploitable |
| `SKIPPED_REF_OUT_OF_SCOPE`, `SKIPPED_IDDOC_NOT_FOUND`, `SKIPPED_SOURCE_REMOVED` | Hors périmètre, IDDOC inconnu, source supprimée à Intraqual |
| `SKIPPED_REF_MANUAL`, `SKIPPED_CATEGORY_EXCLUDED` | Exclusions décidées à la main |
| `MISSING_CHUNKS` | `SUCCESS` sans passage : repris au run suivant |

`is_filtered_by_date = true` signale un document antérieur à 2018 ; son résultat va
dans `chunks_archive`.

## Sélection, parsing et secours (`3_parse`)

**Choix du fichier** (`selection.py`) : pour chaque IDDOC cible, seuls les
dossiers concernés sont listés sur le volume. Les fichiers sont classés par
préfixe (`D_` d'abord, puis `Dm_`), puis par priorité de format, puis par date de
modification la plus récente. Les formats sans priorité sont exclus, ainsi que
quelques fichiers parasites (`thumbs.db`, `desktop.ini`, etc.).

| Format | Priorité |
|---|---|
| docx 1, pdf 2, docm 3, doc 4, html 5, pptx 6, ppt 7, txt 8, xml 9 | 1 à 9 |
| xlsx 10, xls 11, xlsm 12, xlsb 13, rtf 14, odt 15, ods 16 | 10 à 16 |

**Reprise** (`incremental`, mode par défaut) : un IDDOC déjà en statut terminal
est ignoré, sauf si son `indice`, sa date de publication ou son REF a changé, ou
si le hachage du fichier (`document_sha256`) a changé. Les modes `full`
(tout reparser) et `rechunk` (reconstruire les tables à partir du checkpoint,
sans lire le volume) se choisissent par `PARSING_RUN_MODE` (variable `parsing_run_mode`).

**Parsing** (`format_parsers.py`, `image_utils.py`) : Docling sur GPU pour les
formats qu'il lit (PDF, Word, PowerPoint, HTML, texte), avec la structure de
tableaux en mode précis. Le OCR de Docling est désactivé. Les autres formats
passent par des parseurs dédiés : `.doc` par antiword (binaire fourni dans
`data/vendor/antiword/`), `.rtf` par striprtf, `.odt` et `.ods` par odfpy,
`.xls` par xlrd, `.xlsx` et `.xlsm` par openpyxl (repli sur le XML brut du zip),
`.xlsb` par pyxlsb, `.ppt` par la lecture des flux OLE (texte et images JPEG/PNG).
Les `.docx` et `.pptx` lisent leur XML directement quand Docling ne peut pas les traiter.

**Plafonds de taille** (`parse_steps.py`) : 15 Mo pour les formats lus par Docling,
35 Mo pour les PDF, 100 Mo pour les formats lus par les parseurs dédiés et pour
les fichiers OOXML (`.docx`, `.docm`, `.pptx`, `.pptm`). Un `.docx` ou `.pptx`
au-delà de 15 Mo (après retrait des objets incorporés inutiles) passe par la
lecture XML. Au-delà du plafond, le fichier reçoit `SKIP_TOO_LARGE`, sans tentative.

**PDF scannés** : si le texte extrait fait moins de 150 caractères
(`PARSING_LLM_OCR_TEXT_THRESHOLD`), les 100 premières pages au plus sont rendues
en images. Elles sont décrites comme des pages à transcrire intégralement, par
le prompt `PDF_PAGE_OCR_PROMPT`. Les tampons et annotations sont relevés sur une
ligne `STAMP/WATERMARK:`, car ils portent souvent un statut.

**Images** : extraites à une échelle de 3 (environ 216 DPI), en PNG, sur le volume
`{images}/{IDDOC}/{IDDOC}_IMG_{NNN}.png` (rendus de pages : `_PAGE_{NNN}`). L'écriture
est forcée sur le disque (`fsync`). Une image dont le plus grand côté est
inférieur à 140 px, ou dont le plus petit côté est inférieur à 85 px, est
marquée `SKIPPED_DECORATIVE` : elle est illisible, et aucun appel au modèle n'est
fait.

**Retry** : `ENABLE_RETRY` reprend les IDDOC en échec avec leur fichier suivant
(second choix du classement).

**Points de contrôle** : le checkpoint est écrit tous les 100 fichiers
(`CHECKPOINT_BATCH_SIZE`). Une exécution complète (`full`) copie d'abord le
checkpoint existant (clone superficiel nommé `_pipeline_checkpoint_AAAAMMJJ_HHMM`).

## Découpage en passages (`chunking.py`)

Le Markdown est coupé sur les titres de niveau 1 à 3. Un passage ne traverse pas
deux sections de niveau 1 ou 2 (sauf les sections trop petites pour exister
seules). Tailles en tokens (tiktoken, cl100k) : 150 minimum, 300 visés, 450 au
maximum, et 1 600 caractères au plus (le reranker ne lit que les premiers
2 000 caractères). Chevauchement de 12 % entre passages d'une même section. Les
tableaux trop longs sont découpés par lignes, avec l'en-tête répété. Chaque
passage porte son fil de titres (`semantic_headers`) et commence par un préfixe
de provenance :

```
[Source: <REF> | Title: <titre> | Division: <division> | Date de diffusion: <date> | …]
```

Les tables des matières, les pages de garde et les blocs répétés
(`BOILERPLATE_MIN_DOCS` : présents dans au moins 20 documents) sont marqués dans
`chunk_content_type`.

## Descriptions d'images (`4_describe_images`)

- Traite les images `PENDING` et `ERROR`, dans la limite de
  `PARSING_LLM_BATCH_SIZE` (6 000) par exécution. Au-delà, le noyau Python du
  nœud CPU devient instable ; le reste attend le run suivant.
- Une image sans description reste `PENDING`, avec `attempts` incrémenté et
  `last_error_kind` renseigné ; le run suivant la reprend, jusqu'à
  `PARSING_IMAGE_MAX_ATTEMPTS` (10) tentatives, puis elle passe en `FAILED_LIMIT`.
- Appels au modèle `PARSING_LLM_ENDPOINT` (`databricks-gpt-5-6-luna` par défaut) :
  10 en parallèle, avec un limiteur de débit (jetons par minute, requêtes par
  heure). Les résultats sont fusionnés dans `image_metadata` par tranches de
  2 000 (`PARSING_LLM_CHECKPOINT_CHUNK_SIZE`) : une interruption garde le travail
  fait.
- Le prompt `IMAGE_DESCRIPTION_PROMPT` (`config.py`) demande une catégorie (tableau,
  organigramme, schéma, graphique, photo technique, texte), puis une extraction.
  Une image décorative ou déjà couverte par le texte reçoit `SKIP`.
- Statuts : `PENDING`, `DONE`, `SKIPPED` (réponse `SKIP`), `SKIPPED_DECORATIVE`
  (filtre géométrique), `EXTRACTION_FAILED` (fichier image absent), `FAILED_LIMIT`
  (tentatives épuisées).
- Chaque endpoint de la chaîne (`PARSING_LLM_ENDPOINT`, puis
  `PARSING_LLM_FALLBACK_ENDPOINTS`) est essayé sur `PARSING_LLM_ROUNDS` passes,
  avec des attentes entre les passes. Après `PARSING_LLM_BREAKER_FAILURES` images
  en échec consécutives sur un endpoint, celui-ci est écarté pour le reste du run.
  Un 429 ne compte ni comme échec de l'endpoint ni comme tentative de l'image.
- Les jetons de chaque tentative qui a répondu sont comptés, les tentatives en
  échec à part.
- Mode `full` : remet toutes les images en `PENDING`. Le paramètre
  `rebuild_image_chunks` reconstruit les passages d'images à partir des
  descriptions stockées, sans appel au modèle.
- Un passage d'image contient la section, la légende et le préfixe de provenance.
  Seules les descriptions d'au moins 40 caractères, sans `SKIP`, sont indexées.
- La clé de fusion de `image_metadata` est `(IDDOC, image_id)`, celle des passages
  est `chunk_id`. Une fusion ne réécrit que les lignes qui changent.
- Un document `IMAGES_PENDING` dont toutes les images sont décrites passe en `SUCCESS`
  (ou en `SKIPPED_EMPTY_IMAGES` si aucune image n'est informative).
- Le jeton du modèle est lu dans le secret `qualibot/serving_token`. Sans ce
  secret, le notebook utilise son propre jeton.

## Index Vector Search (`5_sync_index`)

- Les index à synchroniser sont listés par cible dans `parsing_vector_search_indexes`
  (`databricks.yml`). Une liste vide ne synchronise rien.
- Seuls `chunks_index` et `chunks_full_index` peuvent être créés par la tâche. Un
  index absent est créé sur l'endpoint `qualibot`, avec le modèle d'embedding
  `databricks-qwen3-embedding-0-6b`, en mode `TRIGGERED`. Tout autre index listé
  est synchronisé mais jamais créé. La tâche attend la fin de la synchronisation
  pendant 45 minutes au plus.
- `chunks_full` réunit `chunks` et `chunks_archive` : tous les documents, quelle
  que soit leur date, pour la recherche d'impact. Il est reconstruit quand
  `parsing_build_chunks_full` vaut `true` ou quand son index est listé. Sur la cible
  `qualibot-uat`, la table est construite sans son index.
- Les tables lues par un index gardent 60 jours de historique de modifications
  (`delta.deletedFileRetentionDuration` et `delta.logRetentionDuration`) : la
  synchronisation incrémentale ne peut pas reprendre au-delà.

## Catalogue des documents (`6_update_kb_metadata`)

Reconstruit la table `doc_catalog` de la base Lakebase indiquée par la variable
`parsing_lakebase_database` (vide : tâche sautée), à partir de `parse_manifest` et des
passages : REF, titre, lien, division, révision (`indice`) et date de publication.
Met aussi à jour `knowledge_base_metadata` (date des documents affichée dans le
chat). L'application relit le catalogue toutes les 30 minutes.

## Tables et volumes

Tables Delta du schéma `<env>_proj.qualibot` (`catalog_schema` des variables) :

| Table | Rôle |
|---|---|
| `parse_manifest` | Documents du jour : REF, titre, révision, date, catégories, `parse_content` |
| `processed_files` | Une ligne par IDDOC : statut, fichier retenu, parseur, erreur, `is_in_rag`, `is_filtered_by_date` |
| `_pipeline_checkpoint` | Résultat de chaque fichier tenté ; relu par toutes les étapes de `3_parse` |
| `chunks` | Passages du chat (texte et images), tous divisions confondues |
| `chunks_archive` | Passages des documents antérieurs à 2018 |
| `chunks_archive_notices` | Un passage-notice par document antérieur, sans contenu |
| `chunks_full` | `chunks` + `chunks_archive` (recherche d'impact), si construit |
| `image_metadata` | Une ligne par image : emplacement, statut, description, jetons |
| `category_reference` | Hiérarchie des catégories et division de chaque catégorie |
| `parsing_run_health` | Une ligne par exécution (tableau de bord) |
| `document_change_log` | Une ligne par document nouveau ou révisé (tableau de bord) |
| `_rag_exclusions`, `_retry_temp` | Tables de travail |

Toutes les tables reçoivent le suffixe `PARSING_TABLE_SUFFIX` (variable
`parsing_table_suffix` : vide, sauf pour une validation où il vaut `_test`). Les
écritures passent par `tables.py`.

Volumes :

| Volume | Usage |
|---|---|
| `/Volumes/prod_landingzone/intraqual/intraqual_documents` | Documents Intraqual, lecture seule |
| `<app_proj_catalog>.<app_proj_schema>.images` (`images_test` en cible de test) | Images extraites, un dossier par IDDOC (variable `parsing_images_volume`) |
| `parsing_offline_models_path` (par cible) | Modèles Docling et d'embedding, hors ligne |

## Variables principales

Variables Databricks (`databricks.yml`) :

| Variable | Défaut | Rôle |
|---|---|---|
| `parsing_run_mode` | `incremental` | `incremental`, `full` ou `rechunk` |
| `parsing_parse_filter` | vide | Liste d'IDDOC séparés par des virgules : exécution ciblée |
| `parsing_table_suffix` | vide | Suffixe des tables en écriture (`_test` pour une validation) |
| `parsing_vector_search_indexes` | vide | Index synchronisés par la tâche 5 |
| `parsing_build_chunks_full` | `false` | Reconstruit `chunks_full` sans son index |
| `parsing_archive_max_docs` | `0` | Nombre de documents antérieurs à 2018 parsés (-1 : tous) |
| `parsing_archive_notices_in_rag` | `false` | Copie les notices des documents antérieurs dans `chunks` |
| `parsing_max_source_staleness_hours` | `36` | Garde de fraîcheur (0 : désactivée) |
| `parsing_gpu_node_type` | `g4dn.4xlarge` | Nœud GPU (T4) |
| `parsing_num_workers` | `2` | Workers GPU |
| `parsing_describe_node_type` | `m5d.xlarge` | Nœud CPU de la description d'images |
| `parsing_schedule_cron`, `parsing_schedule_pause` | `0 0 4 * * ?`, `PAUSED` | Planification (Europe/Paris) et pause |
| `parsing_lakebase_database` | vide | Base Lakebase du catalogue (vide : tâche 6 sautée) |
| `parsing_images_volume` | `images` | Volume des images extraites, dans le schéma du projet |

Constantes de `config.py`, lues dans l'environnement quand elles sont
surchargeables : `PARSING_DOC_DATE_CUTOFF` (`2018-01-01`), `PARSING_TARGET_CHUNK_TOKENS`
(300), `PARSING_MAX_CHUNK_CHARS` (1600), `PARSING_LLM_BATCH_SIZE` (6000),
`PARSING_LLM_ENDPOINT`, `PARSING_EMBED_SOURCE_PREFIX` (`true`).

## Clusters et exécution

- `meta_cpu_cluster` et `describe_cpu_cluster` : mode `SINGLE_USER`, sans worker
  pour les tâches 1 et 2 et pour la description.
- `parse_gpu_cluster` : runtime GPU ML. Le driver est à la demande
  (`first_on_demand: 1`) ; les workers peuvent être en spot. Un GPU par tâche
  (`spark.task.resource.gpu.amount = 1`), donc une parse à la fois par GPU.
- `3_parse` installe `opencv-python-headless==4.12.0.88` par une cellule `%pip`.
  La version 4.13 ne s'importe pas sur ce runtime. Cette cellule ne doit pas être
  remplacée par une bibliothèque du cluster.
- Les vues `gd_*_latest` sont filtrées à distance : `create_proj_parse_manifest.py`
  autorise leurs auto-jointures (`spark.databricks.remoteFiltering.blockSelfJoins = false`).
- Le parallélisme du cluster se lit avec `spark.sparkContext.defaultParallelism`.
- Les tâches tournent sous l'identité de service de leur cible (`run_as` dans
  `resources/parsing_pipeline.job.yml`).
- Aucune politique de cluster n'est attachée (voulu : aucune politique de
  l'organisation ne convient à ces clusters).

## Contrôles qualité (`quality.py`)

`quality.py` contrôle les tables à écrire. Un contrôle `RED` arrête la
tâche sans rien écrire : les tables restent inchangées. Un contrôle
`AMBER` est journalisé, et la tâche continue.

| Table | Contrôle | Niveau |
|---|---|---|
| `category_reference` | Non vide ; `IDDOC` non nul et unique ; `division` sur 99 % des lignes | RED ; AMBER |
| `parse_manifest` | Non vide ; `IDDOC` non nul et unique ; `ref` et `titre` présents sur 99 % des lignes | RED ; AMBER |
| `processed_files` | `IDDOC` non nul et unique ; `parse_status` non nul ; `ref` et `division` présents sur 99 % des lignes | RED ; AMBER |
| `chunks`, `chunks_archive` | `chunk_id` non nul et unique sur les deux tables ; `IDDOC`, `REF` et texte présents ; `division` et `titre` sur 99 % | RED ; AMBER |
| `image_metadata` | `(IDDOC, image_id)` non nuls et uniques ; fichier présent sur plus de 95 % des images | RED ; AMBER |
| passages d'images (`chunks`, `chunks_archive`) | `chunk_id` non nul et unique ; `IDDOC`, `REF` et texte présents ; `division` sur 99 % | RED ; AMBER |
| `doc_catalog` (Lakebase) | Non vide ; `ref` unique ; `title` et `division` sur 99 % ; refus si le catalogue perd plus de la moitié de ses lignes | RED ; AMBER |
| `chunks_full` | Non vide ; `chunk_id` non nul et unique ; `chunk_text` non nul (une source vide viderait l'index) | RED |

La cohérence entre `REF` des passages et `parse_manifest` est signalée (AMBER) sans
bloquer l'écriture.
