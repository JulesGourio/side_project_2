# ROADMAP — chantiers ouverts

Fichier de suivi : cocher / dater au fur et à mesure. Les chantiers terminés sont dans l'historique du dépôt.

## Aperçu exact des DOCX (livré, à connaître)

Le rendu « Exact (PDF) » du Compare convertit le DOCX en PDF avec un LibreOffice **portable** (aucun paquet système n'est
installable sur Databricks Apps, mais un arbre de fichiers s'exécute) : `runbooks/scripts/soffice/package_libreoffice.py`
construit une archive tar.gz (~290 Mo) à partir des .deb officiels, `scan_needed_libs.py` calcule la fermeture transitive des
bibliothèques (lecteur ELF `DT_NEEDED` en Python pur ; 157 .so Ubuntu jammy fusionnés dans `program/`, 317 Mo une fois
extrait). L'archive est déposée dans le volume UC, téléchargée et extraite une fois par process au premier aperçu
(`server/services/compare/soffice.py`, variable `SOFFICE_ARCHIVE_VOLUME_PATH`, dossier d'extraction versionné par nom
d'archive : changer le nom de fichier force la réextraction). `POST /api/preview/pdf` convertit (cache disque par hash de
contenu), le client affiche le PDF (`client/src/components/shared/ExactDocxPreview.tsx`) ; si le moteur est absent l'endpoint
répond 501 et l'UI bascule sur `docx-preview` (mode « Fast (DOCX) »). Le même moteur sert aux PPTX
(`conversion.py::_libreoffice_cmd`, repli python-pptx). Vérifié sur `qualibot-uat-test` en juillet. Reste : la config prod
(archive à déposer dans le volume prod).

## Impact search

- [ ] **Jeu d'évaluation de l'impact search.** 5 à 10 comparaisons réelles pour lesquelles on connaît les documents réellement à
  mettre à jour ; un script rejoue la recherche et compte les documents oubliés et les faux positifs. À lancer avant tout
  changement de prompt, de modèle ou de réglage (`COMPARE_IMPACT_*`). Les votes 👍/👎 par document (`impact_feedbacks`)
  serviront à constituer ce jeu.
- [ ] **Documents qui citent la référence du document modifié.** La recherche retrouve un document s'il reprend le contenu modifié
  (« 12 N·m »). Elle rate celui qui écrit seulement « serrer selon PR-2207 » sans reprendre la valeur. Piste : une recherche en
  plus sur la référence elle-même, affichée à part (« Documents that reference PR-2207 »).
- [ ] **Colonne `#` dans l'export Excel de la Change Table**, pour retrouver les mêmes numéros C1, C2… que dans l'impact search.
- [ ] **Mode « décrire un changement à la main »** : désactivé volontairement (`IMPACT_MANUAL_MODE_ENABLED`), à réactiver si le besoin revient.
- [ ] Qualité du juge impact V3 et couverture `query_hits` après quelques usages réels (les données sont dans `impact_requests`,
  Lakebase, inaccessible en direct depuis le poste : passer par un notebook serverless).

## Chatbot et parsing

Configuration retenue le 2026-10-08 et tout ce qui a été mesuré : `docs/chat_tests.md`. Reste à tester : fiche par document (E1),
contexte par passage (E2), index sans préfixe, pistes de recherche R1–R12, documents d'avant 2018, numéros de page (§ E et § 7 de ce document).

- [ ] Chat : perte de réponse partielle en mode pont de traduction si erreur en cours de flux ; désynchronisation possible si le
  réseau coupe entre la persistance et l'événement `done`. Rares, correctifs risqués sans tests d'intégration.
- [ ] `_caps_cache` / capabilities : re-vérifier le fail-open quand la table `users` sera peuplée en prod.

## Comparaison de documents (limites connues, audit 2026-10)

Détail dans `docs/compare_audit_2026-10.md`.

- [ ] **Tableau PDF sans bordures** : une valeur qui change de colonne reste invisible, et les colonnes ne sont pas nommées.
- [ ] **PDF sur deux colonnes** : le diff est juste, mais le rattachement aux sections peut être faux.
- [ ] **`.xls`** : accepté à l'envoi, mais l'analyse échoue (paquet `xlrd` absent) avec un message demandant de convertir en `.xlsx`.
- [ ] **En-têtes et pieds de page DOCX** : toujours non lus. Formes DrawingML (`wps:`) sans fallback VML non détectées (rare). Notes de
  présentateur PPTX non extraites (choix délibéré : bruit).
- [ ] **14 autres points** listés dans la partie B du rapport d'audit, dont le cache d'analyse écrit par le navigateur et les fichiers `.tmp` jamais nettoyés.
- [ ] **Mesure automatique de la qualité de détection** : le corpus d'évaluation (paires de documents annotées, rappel / précision du
  diff, seuil de similarité 0,55 tranché le 2026-07-17) n'est pas dans ce dépôt, et le mode `--score` de `runbooks/scripts/compare/compare_preview.py` a été
  supprimé avec lui. À versionner (ou un sous-ensemble) pour que chaque changement de moteur soit mesurable.

## Divers

- [ ] Repo de référence des notebooks (hors de ce dépôt) : le dossier `dev_qualibot` monté dans le workspace (clone bare, filtre blobless)
  affiche des milliers de fichiers « supprimés » via `git status` ; un `git reset --hard` dessus a échoué à mi-chemin. Contournement :
  un clone local propre et un Repo Databricks séparé (`dev_qualibot_clean`). **Ne pas retenter d'opération git en masse sur l'ancien
  dossier** (ni `git add -A`, ni `reset --hard`, ni merge complet) ; à reclôner proprement un jour.
