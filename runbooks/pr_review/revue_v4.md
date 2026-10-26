# Revue v4 (2026-10-26) — état réel, constat par constat

La revue v4 (« QualiBOT Full Repository Review », commit `3910813` de `main` sur Bitbucket) dit que 2 constats critiques et
6 hauts restent ouverts. Chaque constat a été relu dans le code de la branche `claude/sharp-mayer-uqwx8x`, qui est la
référence des corrections.

## 1. Pourquoi la v4 voit des problèmes déjà corrigés

- Elle cite `server/routers/chat.py:548-572`. Ce fichier n'existe plus depuis le 2026-10-13 : le chat est dans
  `server/routers/chat/ws.py`, `sessions.py`, `persistence.py` et `helpers.py`. La v3 du 2026-10-23 citait déjà `ws.py`.
- Les numéros de ligne de H1 à H6 (`user.py:126-129`, `parse_steps.py:269-279`…) et leurs textes sont ceux de la v3, recopiés.
- Elle dit qu'aucun commit ne traite ces constats ; les corrections sont dans le commit `867ac65` du 2026-10-23 et les suivants.

Deux explications possibles, à vérifier par Jules (section 4) : soit le `main` de Bitbucket n'a pas reçu le code de la
branche, soit la revue a été écrite sans relire le code actuel.

## 2. Constat par constat

| v4 | Constat | État dans le code | Preuve |
|---|---|---|---|
| C1 | Le WebSocket ouvre le chat si la recherche des droits lève une exception | Corrigé : exception = refus | `ws.py:76-80`, test `test_capability_lookup_failure_refuses_the_socket` |
| C1 bis | Si la table `users` ne peut pas être lue et qu'aucun groupe n'est trouvé, le visiteur a le chat | Choix de Jules (2026-10-23) : le chat est ouvert à tout visiteur de l'app, la permission de l'app filtre | `user.py:132-136`, README racine « Design decisions » |
| C2 | Un `session_id` d'un autre utilisateur permet d'écrire dans sa conversation | Corrigé : l'enregistrement vérifiait déjà le propriétaire ; le tour est maintenant refusé avant tout appel au modèle (2026-10-26). L'historique n'est jamais lu côté serveur : le navigateur envoie le sien | `ws.py:115`, `persistence.py:37`, deux tests |
| H1 (N1) | `POST /history` accepte le dossier d'un collègue | Corrigé : chemin gardé seulement sous la racine de l'appelant ou déjà nommé par une ligne de l'appelant | `history.py:72` |
| H2 (N2) | Petit fichier en échec sauté pour toujours | Corrigé : erreur testée d'abord ; OCR en attente jamais sauté | `decisions.py:24` |
| H3 (N3) | Ancien texte ramené sous la nouvelle révision | Corrigé : priorité aux lignes du sha de la dernière tentative | `parse_steps.py:390` |
| H4 (N4) | Lecture de périmètre vide = tables vidées | Corrigé : arrêt avant toute suppression si le périmètre baisse de plus de 30 % | `create_proj_parse_manifest.py:273` |
| H5 (N5) | Chat ouvert, pas d'origine en PROD, anonyme accepté | Corrigé pour l'origine (PROD renseignée, liste vide = refus hors dev) et l'anonyme (refusé hors dev) ; chat ouvert = choix de Jules | `ws.py:56`, `ws.py:88`, `target_env.json` |
| H6 (N6) | Attributs XML ignorés | Corrigé : une ligne `tag@attr=valeur` par attribut | `xml.py:44` |
| N7 | Limite de débit absente du chat et de l'aperçu | Corrigé | `ws.py:94`, `preview.py:30,66` |
| N8 | full/rechunk effacent les lignes SKIPPED | Corrigé (option a de Jules) | `output_steps.py` `_replace_processed_rows` |
| N9 | Révisions remplacées remises en full | Corrigé | `parse_steps.py` `_superseded_iddocs` |
| N10 | Dossiers vides journalisés NEW chaque jour | Corrigé (option C de Jules : NEW une seule fois) | `output_steps.py:175` |
| N11 | Copie Delta drop/recrée, liste d'exclusion | Corrigé : `CREATE OR REPLACE`, anciens fichiers supprimés | `import_lakebase_tables_to_delta_job.py`, `export_lakebase_to_volume_job.py` |
| N12 | Droits synchronisés dans l'export | Corrigé : job `lakebase_user_capabilities` toutes les 2 h | `resources/lakebase_sync.yml` |
| N13 | Recherche d'impact coupe des changements | Corrigé : les changements non cherchés sont listés à l'utilisateur | `impact_queries.py` `left_out` |
| N14 | Clé de cache incomplète | Corrigé : prompt réellement envoyé + endpoint ; réponse de secours non mise en cache | `analyze.py:113,442` |
| N15 | Filtre d'historique du client | Corrigé : filtrage par paires | `client/src/components/chat/history.ts` |
| N16 | Colonne insérée en milieu de feuille | Corrigé : alignement par nom d'en-tête | `excel.py` `_align_columns` |

Ajouté hors revue (2026-10-26) : `run_mode` `full` ou `rechunk` avec un `parse_filter` est refusé par `3_parse`
(ces modes réécrivaient l'index avec les seuls documents filtrés).

Suite de tests : 780 réussis, 0 échec (local).

## 3. Ce qui reste ouvert (connu, décidé ou reporté)

| Sujet | Statut |
|---|---|
| Chat ouvert à tout visiteur de l'app (C1 bis, H5) | Choix de Jules, confirmé le 2026-10-26 malgré la revue v4 |
| Jeton longue durée de la CI → OIDC ; « Production » limité à `main` | Reporté par Jules |
| Bibliothèques des clusters du parsing | Figées le 2026-10-26 sur les versions relevées par Jules sur un 16.4 (sauf `numpy<2` et `pydantic` de la tâche GPU, autre runtime) |
| `uv.lock` du dépôt de Jules | `uv lock` bloqué par le proxy (certificat) : relancer avec `--native-tls` |
| Cluster GPU sans politique | Aucune version GPU dans la politique S |
| Tableau de bord DEV sur d'anciennes tables | Reporté par Jules |
| Tableaux DOCX des en-têtes et pieds de page | Reporté par Jules |
| 9 remarques de Yann | En attente de Yann |

## 4. Vérifier que `main` (Bitbucket) contient les corrections

Dans le dépôt de Jules, sur `main`, en PowerShell (aucun droit Databricks) :

```powershell
git fetch origin
git checkout main
git pull
Test-Path server\routers\chat.py
Select-String -Path server\routers\chat\ws.py -Pattern "session_of_another_user", "caps = {'can_chat': False}"
Select-String -Path server\routers\history.py -Pattern "_accepted_session_path"
Select-String -Path utils\parsing_pipeline\decisions.py -Pattern "filter_would_empty_tables", "scope_dropped_too_much"
```

Attendu : `False` pour la première ligne, puis une ligne trouvée pour chaque motif. Sinon, `main` n'a pas reçu le code
de la branche et la revue v4 a raison sur ce dépôt.
