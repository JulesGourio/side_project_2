# ROADMAP — suivi des prochains chantiers

Fichier de suivi vivant : cocher / dater au fur et à mesure. Les features
elles-mêmes sont documentées dans `docs/{COMPARE,CHATBOT}.md`.

## P1 — fidélité du rendu docx/pptx dans l'app

- [x] **DOCX rendu EXACT — RÉSOLU 2026-07-16 avec LibreOffice portable → PDF.**
  Le verrou de 2026-07-15 (« pas de paquets système sur Databricks Apps »)
  bloque l'*installation*, pas l'*exécution* : un arbre LibreOffice portable
  (tar.gz ~290 Mo, construit par `utils/soffice_packaging/package_libreoffice.py`
  depuis les .deb officiels TDF — extraction ar/tar 100 % Python, tourne sous
  Windows) est déposé dans le Volume UC et téléchargé/extrait une fois par
  process au premier preview PDF (`server/services/soffice.py`,
  `SOFFICE_ARCHIVE_VOLUME_PATH`). `POST /api/preview/pdf`
  convertit un docx en PDF (cache disque par hash de contenu) et le
  client l'affiche dans le viewer PDF natif du navigateur
  (mode « Exact (PDF) », défaut). Fallback automatique sur docx-preview
  (mode « Fast (DOCX) », avec zoom + scroll synchronisé) si le moteur est
  absent — l'endpoint répond 501 et l'UI bascule en expliquant pourquoi.
  Vérifié en local (soffice.com Windows via `SOFFICE_PATH`) sur
  A321_OWD_FINISHING_STEP_2 : 23 pages, cartouche/images CAD/tableaux
  fidèles, ~20-30 s la première conversion puis cache instantané.
  **Vérifié en conditions réelles sur qualibot-uat-test (2026-07-16, via
  Playwright)** : preview docx du Compare rendu
  pixel-fidèle dans le viewer natif. Il a fallu 3 itérations de tarball :
  v1 = arbre TDF nu (échec `libXinerama.so.1`), v2 = + libs X11
  (échec `libssl3.so` — le NSS, pas OpenSSL), v3 = fermeture transitive
  complète calculée par `utils/soffice_packaging/scan_needed_libs.py`
  (lecteur ELF DT_NEEDED pur Python, 157 .so Ubuntu jammy fusionnés dans
  `program/`, 317 Mo). L'archive v3 est dans les deux volumes
  (`doc_compare/libreoffice/` et `test/libreoffice/`) ; le tarball est
  re-extrait par process dans un dossier versionné par nom d'archive
  (changer le nom de fichier ⇒ réextraction garantie). Le Compare a le même
  rendu exact via `POST /api/preview/pdf` +
  `client/src/components/shared/ExactDocxPreview.tsx` (toggle Exact/Fast
  dans la modale). Restent hors scope : déploiement uat/prod (archive déjà
  en place pour uat ; config prod à renseigner).
- [x] **DOCX — RÉSOLU 2026-07-15 avec `docx-preview` (npm, Apache 2.0).**
  Cause confirmée par test réel sur qualibot-uat-test : `docx_to_html_body`
  (mammoth) convertit vers du HTML sémantique "propre" par conception, pas
  un rendu de mise en page. Recherché la best practice avant d'implémenter :
  doc officielle Databricks confirme que **les Apps ne supportent que 3
  mécanismes de dépendances** (`requirements.txt`/`pyproject.toml` Python,
  `package.json` Node) — **aucun mécanisme pour des paquets système** (pas
  d'apt, pas de Dockerfile, pas d'init script), contrairement aux clusters
  Databricks classiques. LibreOffice (binaire système ~300-400 Mo) n'est donc
  **pas installable sur Databricks Apps** — option écartée.
  `docx-preview` est 100% client-side (aucun binaire, aucun téléchargement
  manuel — juste une dépendance `client/package.json` de plus), rendu
  vérifié en direct sur l'app déployée : vraie mise en page "page", tableaux
  avec bordures/couleurs, images bien positionnées — net progrès vs mammoth.
  Nouveau composant `client/src/components/shared/DocxPreview.tsx`, branché
  dans Compare (`CompareView.tsx`, rendu depuis les bytes déjà en mémoire
  côté client — plus d'appel à `/api/preview` pour les docx).
- [x] **PPTX — VÉRIFIÉ E2E sur qualibot-uat-test (2026-07-17, Playwright).**
  Le branchement du 2026-07-16 (`conversion.py::_libreoffice_cmd` →
  `soffice.find_soffice()`, arbre portable du Volume UC) fonctionne en réel :
  upload d'un vrai pptx (PPT 4 slides de la session Compare du 2026-06-30)
  dans la modale de preview → rendu pixel-fidèle slide par slide (fonds
  dégradés, icônes, mise en page). Le fallback python-pptx reste en place.

## P0 — en cours / bloquants

- [x] **Impact search depuis qualibot-uat-test (403 Vector Search) — RÉSOLU 2026-07-15.**
  Grants appliqués via le notebook `Grant_Permissions_App_Qualibot`
  (`leap-dbx-repository`, `platform/app_grants/`, branche `dev-qualibot`,
  PR #435 ouverte vers main) : USE_CATALOG sur `uat_landingzone`, USE_SCHEMA +
  SELECT sur tout le schéma `uat_landingzone.qualibot` (couvre `chunks_v2` et
  `chunks_index_v2` sans grant par table), READ/WRITE_VOLUME sur un volume
  **isolé par app** (qualibot → `doc_compare`, qualibot-uat-test → `test`,
  jamais les deux sur la même app — évite qu'un run de test touche un document
  de prod). SP résolus par nom (SCIM) plutôt que par UUID en dur. Widget
  `target_app` (both/qualibot/qualibot-uat-test) pour appliquer les grants à
  une seule app à la fois. Appliqué pour de vrai le 2026-07-15 via le job de
  test `670025070898003` (`run_as` un SP dédié — nécessaire car ni Jules ni
  ce SP n'ont MANAGE sur `uat_landingzone.qualibot`, propriété de
  patrice.puntis@latecoere.aero). **Vérifié en conditions réelles** : Compare
  (V1/V2/V3 impact search) et Chat tournent sans erreur sur l'app déployée —
  voir notes du 2026-07-15.
- [x] **Push de la branche `impact-search-on-images-report`** — fait (voir
  historique du repo qualibot ; le point EN COURS maintenant est la PR #435
  de `dev-qualibot` → `main` sur `leap-dbx-repository`, **à faire par Jules
  uniquement** — repo platform de l'équipe, revue/merge humain, ne pas
  automatiser ; rappel 2026-07-17).

## P1 — jeu d'évaluation DocCompare

- [x] **6 paires réelles sélectionnées et annotées (2026-07-17)** —
  `utils/compare_eval/` : 3 schémas de câblage WDT (re-brochages, insertions
  de feuilles), NE07-011 J→K (norme Embraer, rev K = Revision Report seul),
  AIPI03-11-001 A6→A9 (spéc process Airbus), QP-1299→INAQ604 (remplacement
  de document, cas de stress). Références `refs/*.json` versionnées
  (`status=proposed` — **à corriger/valider par Jules**), annotation
  assistée par les `analysis.md` historiques des sessions du volume et
  vérifiée sous-chaîne par sous-chaîne contre le diff ; PDF non versionnés
  (`fetch_pairs.py` les retélécharge).
- [x] **Mode scoring livré (2026-07-17)** — `utils/compare_preview.py
  --score refs/<pair>.json [--threshold X]` : rappel/précision du diff
  contre la référence, sans token. Baseline : rappel **68/68 (100 %)** —
  l'unique MISS initial (renvoi harnais 7089, WDT653) s'est avéré NE PAS
  être un trou du moteur : texte identique dans les deux PDF (vérifié
  PyMuPDF), l'entrée venait de l'analyse LLM de juin (graphique ou
  hallucination), déplacée en `rejected_candidates`. Précision 0-4 %
  dominée par le cartouche répété par page (catégorisé `must_not_flag`) —
  traité ensuite par le dédoublonnage ci-dessous.
- [x] **Décision seuil 0,55 : TRANCHÉE sur le jeu d'éval (2026-07-17) —
  garder 0,55.** Sweep 0,40/0,55/0,70 : 0,40 → 91 % de rappel
  (sur-appariement, des exigences AIPI disparaissent dans des MODIFIED
  absorbants), 0,55 → 99 %, 0,70 → 83 % (effondrement sur les re-brochages
  câblage, WDT403 à 25 %). Le filtrage des changements mineurs est un
  problème distinct du seuil : le bruit dominant est le cartouche répété
  par page — piste = dédoublonnage/filtre dédié, voir
  `utils/compare_eval/README.md`.
- [x] **Dédoublonnage du boilerplate fait dans la foulée (2026-07-17)** —
  `_diff_engines.py::_collapse_page_repeats` : un changement identique
  répété sur ≥3 pages (cartouche de révision, champ de classification…)
  devient UNE entrée annotée « repeated on N pages » ; fragments de
  pagination (`Page N`, `N of M`) masqués pour le regroupement seulement
  (jamais les autres nombres — les pins de câblage restent distincts) ;
  lignes de sommaire à dot-leaders repliées en une entrée récapitulative.
  Mesuré sur le jeu d'éval, **rappel inchangé (68/68)** : entrées −15 % à
  −53 % selon la paire (WDT653 580→271, WDT403 2 780→1 660), taille de diff
  −13 % à −51 % → moins de tokens par analyse ET requêtes V2/V3 (tronquées
  à 20k chars) plus denses en signal. Réglable par
  `COMPARE_BOILERPLATE_MIN_PAGES`. Tests unitaires ajoutés
  (`test_compare_pipeline.py`), 55/55 verts.

## P1 — nettoyage doc (2026-07-15)

- [x] `docs/TRANSLATE_TAB_BUILD.md` était référencé (README.md, TRANSLATION.md)
  mais avait été supprimé dans un commit antérieur (contenu consolidé ici) —
  liens morts retirés des deux fichiers.
- [ ] Repo `leap-dbx-repository` (platform, hors `latec-compare`) : le dossier
  `dev_qualibot` checkout à `/Workspace/Users/.../dev_qualibot` (clone bare,
  filtre blobless) affiche des milliers de fichiers en état "supprimé" via
  `git status` en shell — s'est avéré être un vrai artefact de ce montage
  Databricks (notebooks + FUSE), pas un faux signal : une tentative de `git
  reset --hard` dessus a réellement échoué à mi-chemin (`RESOURCE_ALREADY_EXISTS`
  sur un fichier Finance). Contournement adopté : un vrai clone local
  (`C:\Users\L0041770\Desktop\GenAI\leap-dbx-repository`, remote identique) +
  un Repo Databricks natif séparé (`dev_qualibot_clean`,
  `/Workspace/Users/jules.gourio.external@latecoere.aero/dev_qualibot_clean`,
  id `3198199749331240`) pour tout ce qui touche `platform/app_grants/` côté
  Qualibot. **Ne pas retenter d'opération git en masse sur l'ancien dossier
  `dev_qualibot`** — ni `git add -A`, ni `reset --hard`, ni un merge complet.
  À nettoyer/reclôner proprement un jour, hors urgence.

## Plus tard — impact search (noté 2026-10-05)

- [ ] **Jeu d'évaluation de l'impact search.** 5 à 10 comparaisons réelles pour
  lesquelles on connaît les documents réellement à mettre à jour ; un script
  rejoue la recherche et compte les documents oubliés et les faux positifs.
  À lancer avant tout changement de prompt, de modèle ou de réglage
  (`COMPARE_IMPACT_*`). Les votes 👍/👎 par document (`impact_feedbacks`)
  serviront à constituer ce jeu.
- [ ] **Documents qui citent la référence du document modifié.** La recherche
  retrouve un document s'il reprend le contenu modifié (« 12 N·m »). Elle rate
  celui qui écrit seulement « serrer selon PR-2207 » sans reprendre la valeur :
  il dépend pourtant du document modifié. Piste : une recherche en plus sur la
  référence elle-même, affichée à part (« Documents that reference PR-2207 »).
- [ ] **Colonne `#` dans l'export Excel de la Change Table**, pour retrouver les
  mêmes numéros C1, C2… que dans l'impact search.
- [ ] **Mode « décrire un changement à la main »** : désactivé volontairement
  (`IMPACT_MANUAL_MODE_ENABLED`), à réactiver si le besoin revient.

## Plus tard — Chat VSI et parsing (audit 2026-10-07)

Tout est dans `docs/chat_vsi_audit_2026-10.md` : essais mesurés (golden et recherche seule),
modèles testés et non testés, pistes de recherche côté app (R1–R12), audit du pipeline de
parsing (P1–P16 : titres de section faux après fusion, sections mélangées, pas de
chevauchement, images, métadonnées manquantes…), enrichissements de l'index (E1–E5),
requêtes de diagnostic (Q1–Q8) et ordre proposé. Rien n'est décidé : à trier avec l'utilisateur.

## Plus tard — comparaison de documents (limites connues, audit 2026-10)

Détail dans `docs/compare_audit_2026-10.md`.

- [ ] **Tableau PDF sans bordures** : une valeur qui change de colonne reste
  invisible, et les colonnes ne sont pas nommées.
- [ ] **PDF sur deux colonnes** : le diff est juste, mais le rattachement aux
  sections peut être faux.
- [ ] **`.xls`** : accepté à l'envoi, mais l'analyse échoue (paquet `xlrd`
  absent) avec un message demandant de convertir en `.xlsx`.
- [ ] **En-têtes et pieds de page DOCX** : toujours non lus.
- [ ] **14 autres points** listés dans la partie B du rapport d'audit, dont le
  cache d'analyse écrit par le navigateur et les fichiers `.tmp` jamais
  nettoyés.

## Veille (connu, non urgent)

Revue le 2026-07-17 : ces quatre points restent volontairement en veille —
aucun n'est actionnable proprement aujourd'hui. V3/`query_hits` attend des
usages réels à observer (les données sont dans `impact_requests`, Lakebase,
inaccessible en direct depuis le poste — passer par un notebook serverless
le jour venu) ; le correctif chat mid-stream est jugé plus risqué que le
bug sans tests d'intégration ; `wps:` sans fallback est rarissime (Word
génère quasi toujours le fallback) et les notes PPTX sont un choix
délibéré ; le fail-open `_caps_cache` ne se re-vérifie qu'avec une table
users peuplée en prod — qui n'existe pas encore.

- [ ] Qualité du juge impact V3 et couverture `query_hits` après quelques
  usages réels.
- [ ] Chat : perte de réponse partielle en mode pont de traduction si erreur
  mid-stream ; désync possible si le réseau coupe entre persistance et
  événement `done`. Rares, correctifs risqués sans tests d'intégration.
- [ ] DOCX : formes DrawingML (`wps:`) sans fallback VML non détectées (rare,
  Word génère quasi toujours le fallback). Notes de présentateur PPTX non
  extraites (choix délibéré — bruit).
- [ ] `_caps_cache` / capabilities : re-vérifier le fail-open quand la table
  users sera peuplée en prod.

## Notes

- 2026-07-15 : **qualibot-uat-test validé de bout en bout sur l'app déployée**
  (pas seulement en local) après application réelle des grants : Compare
  (upload FI_252A90008200 old/new, Impact Analysis → 1 changement détecté,
  V2 15 docs/0 échec, V3 12 candidats jugés €0.026), Chat (question FR,
  réponse + sources + citations, historique persistant). Déploiement fait via
  `deploy_qualibot.ps1 -AppEnv uat-test`. Coût session : job de grants
  (gratuit, pas de LLM) + Compare (Sonnet, ~qq centimes).

- 2026-07-11 : **impact search validé de bout en bout en local** (app lancée
  avec les credentials utilisateur → index accessible) : analyse FI252 29
  lignes, V2 4 s / 7 requêtes / 0 échec / 105 chunks / 15 docs (top :
  IF20335_FR hits=5/7), V3 14 s, jugements motivés, 0,03 €. Aucune erreur de
  concurrence sur l'endpoint d'embedding (parallélisme 3 + retry). Le 403
  ne concerne que le SP de qualibot-uat-test. Bonus : IF20335_FR / IF20335_GB
  remontés côte à côte = exemple concret du pattern « même REF + suffixe de
  langue » à exploiter pour le glossaire (P1).

- 2026-07-11 : chunking parallélisé (×3) après coupure gateway constatée en
  UAT sur NAS410 (4 parties séquentielles ≈ 10 min > durée max de connexion).
  Run validé : 156 s, 191 lignes JSON valides.
- Coût des smoke tests UAT du 2026-07-11 : ~0,63 € Sonnet.
