# Normalisation des codes : ce qui a changé (commit b7581b3)

Diff complet : [7bd0d6b...b7581b3](https://github.com/julesgourio/side_project_2/compare/7bd0d6b...b7581b3). Ton architecture n'a pas bougé : même module ([code_normalization.py](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py)), mêmes fonctions publiques (`normalize_codes`, `normalize_query`, `find_codes`, `canonical_codes`, `cited_codes`), mêmes points d'appel (pipeline `chunk_preprocessed` / `cited_codes`, chat, recherche d'impact).

## 1. Ce qui change dans le module

| # | Avant | Maintenant | Où |
| --- | --- | --- | --- |
| 1 | 56 familles « préfixe-nombre » générées (78 % des REF) | + un **bloc catalogue généré** : toutes les clés de toutes les REF (8 361), lettres et types des codes site, types LdB, phases, signatures | [bloc généré](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L84) |
| 2 | Une seule méthode : regex par famille | **3 détecteurs** fusionnés (le plus tôt, puis le plus long, puis le catalogue gagne) : `CATALOG` (clé du catalogue écrite n'importe comment), `REF_SHAPE` (forme de REF hors catalogue, gardée seulement si signature connue : `SF-1027` comme `SF-1029`), `EXTRA_PATTERNS` | [_catalog_matches](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L1182), [_ref_patterns](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L1271), [_resolve](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L1324) |
| 3 | `EXTRA_PATTERNS = []` | **Rempli** : normes (STD-01), ATA, avions (liste fermée), Part/CS/AMC/GM/21G, alliages, sigles à points (`E.A.S.A`), SAP avec tiret, PN en groupes, `8-D`/`5-S` | [EXTRA_PATTERNS](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L995) |
| 4 | Pas de codes site, LdB, phases, numériques | `Q0697MI`, `LdB IT-243` (jamais sans `LdB`), `PRO-R80`, `19 --058`, `777F-…`, `ŘLCZ` | bloc généré |
| 5 | IF = famille générique | IF a sa propre regex : `IF 2.03.08` oui, `IF 02/08/2001` et `IF 2.02` non | [FAMILY_OVERRIDES](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L964) |
| 6 | `PR/LAT/506`, `IN/AQ/559` ratés | Préfixes écrits en morceaux | [SPLIT_PREFIXES](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L967) |
| 7 | Séparateur d'un caractère | Tirets Unicode, `SF – 1027`, `MI -1365`, accents retirés (`ŘLCZ` → `RLCZ`) | |
| 8 | Minuscules acceptées seulement collées dans les questions | Questions : `sf 1027` accepté, sauf préfixes qui sont des mots (`if`, `go`, `mo`, `le`…). **Passages : `mi-1365` accepté si c'est une clé du catalogue** | [QUERY_STOP_PREFIXES](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L976) |
| 9 | Crochet juste après le code | Crochet après l'identifiant entier : `IP-029-005 [IP029]`, `MI-13991Table [MI13991]` | [normalize_codes](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L1471) |
| 10 | — | Clé partagée par plusieurs REF → elle-même (`QIS45D008`) ; clé qu'une seule REF complète → cette REF (`QIS-46A-352` → `QIS46A352A`) ; `MM-1059` → `MM1059IS` si seule `MM-1059-IS` existe | [catalog_keys](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L1610) |
| 11 | — | Garde-fous : clé du catalogue ≥ 4 caractères (pas `R05`, `M01`), clé tout chiffres seulement avec séparateur (pas le nombre `19001`), mots écrits lettre par lettre ignorés (`D.E.S`), code court dans une chaîne ignoré (`A30` dans `145.A.30`) | [_MIN_DIGIT_KEY](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L1060), [STOP_CODES](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L980) |
| 12 | Langue retirée une fois | Toutes les langues finales retirées (`INE028_FR_EN` → `INE028`) | [ref_canonical](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L1573) |
| 13 | — | `NORMALIZATION_VERSION = 2`, `explain_codes` (dit quel détecteur a trouvé le code), `catalog_drift`, `use_catalog` (tester un autre catalogue sans réécrire le fichier) | [L47](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L47), [explain_codes](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L1424) |
| 14 | CLI imprime le bloc à coller | `python code_normalization.py refs.txt` **réécrit le bloc dans le fichier** | [catalog_from_refs](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/code_normalization.py#L1651) |

## 2. Ce qui change ailleurs

| Fichier | Changement |
| --- | --- |
| [doc_catalog.py](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/server/services/infra/doc_catalog.py#L56) (app) | `_canon` appelle `ref_canonical` du module : même forme canonique pour les liens REF, chips et `doc_references` (accents, plusieurs suffixes de langue) |
| [create_proj_doc_catalog.py](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/create_proj_doc_catalog.py#L296) (tâche 6) | Contrôle AMBER : liste les REF de `doc_catalog` inconnues du module (le run continue) |
| [f3_dictionnaire_codes.py](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/runbooks/retrieval/f3_dictionnaire_codes.py) | Plus de copie du matcher : exécute le vrai module sur le catalogue du jour, exporte les REF, compare à f2 |
| [test_code_normalization.py](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/tests/test_code_normalization.py) | 142 tests sur un petit catalogue fixe (cas des 2 audits Genie et de l'ancien prototype) |
| [README du pipeline](https://github.com/julesgourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/utils/parsing_pipeline/README.md) | Section `chunk_preprocessed` réécrite |

## 3. Choix que tu peux ne pas aimer (et comment revenir)

| Choix | Pourquoi je l'ai fait | Si tu n'en veux pas |
| --- | --- | --- |
| Catalogue (≈ 125 Ko) **écrit dans le fichier .py** | Le module doit rester sans dépendance (exécuteurs Spark + app) | Le lire depuis un JSON à côté du module (à expédier aux exécuteurs) |
| Catalogue actuel = **copie de juillet** (7 600 REF) | Pas d'accès Databricks | Tu m'envoies le CSV de f3, je régénère |
| Minuscules acceptées dans les **passages** pour les clés du catalogue (`mi-1365`) | Écrites ainsi dans des documents | Une ligne à remettre : majuscules seulement en mode passage |
| `sf 1027` (minuscule + espace) dans les questions | Questions réelles du chat | Retirer la règle, garder seulement `sf-1027` |
| Avions, alliages, normes, SAP, PN annotés | Codes cherchés par mot-clé comme les REF | Retirer la ligne de `EXTRA_PATTERNS` correspondante |
| `A350-900` → `[A350900]` (pas `A350`) | Le code tel qu'écrit | Clé limitée au modèle (`A350`) |
| Clé ambiguë → elle-même ; complétion unique → la REF | Comme f2 (Tour 5) | Toujours la clé écrite, sans complétion |
| `_canon` de l'app aligné sur le module | Sinon `doc_references` et le catalogue divergent sur `ŘLCZ`, `_FR_EN` | Revenir à l'ancienne regex (2 lignes) |
| Coût : ~5 ms par passage de 2 Ko (≈ 15 min CPU pour 85k passages, réparties sur les exécuteurs) | 3 détecteurs | Optimisable si gênant |

## 4. Rien n'est appliqué tant que

Les passages ne changent qu'après un `run_mode=rechunk` + synchro de l'index en DEV, que je ne te demanderai qu'après validation de f3. L'index de l'app ne bouge pas.
