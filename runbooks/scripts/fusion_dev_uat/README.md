# Fusion DEV + UAT du parsing (une page)

Pour chaque document, le meilleur résultat de parsing des deux environnements est gardé des deux côtés (Docling avant XML, puis plus d'images, puis plus de texte). Pas de reparsing, pas d'appel LLM. `chunks` et l'index ne sont pas touchés par les notebooks : le run incrémental final ne reconstruit que les documents changés. Chaque table modifiée est sauvegardée (`*_before_merge`).

Toutes les commandes se lancent depuis la racine du dépôt, en PowerShell. Les notebooks sont importés dans `/Workspace/Shared/qualibot_fusion` (DEV et UAT).

| # | Quoi | Où |
|---|---|---|
| 1 | `.\runbooks\scripts\fusion_dev_uat\fusion.ps1 import` | ta machine |
| 2 | Run all `1_DEV_export` | DEV |
| 3 | Run all `1_UAT_export` | UAT |
| 4 | `.\runbooks\scripts\fusion_dev_uat\fusion.ps1 snapshots` | ta machine |
| 5 | Run all `2_DEV_fusion` | DEV |
| 6 | Run all `2_UAT_fusion` (affiche le plan, n'écrit rien), envoyer le tableau à Claude, puis mettre `APPLY = True` en haut et relancer | UAT |
| 7 | `.\runbooks\scripts\fusion_dev_uat\fusion.ps1 images` (environ 1 h) | ta machine |
| 8 | Jobs > `D_1_Qualibot_Parsing_Pipeline` > Run now, paramètres par défaut | DEV puis UAT (UAT hors heures d'usage) |

Tous les notebooks tournent avec ton compte. S'il manque un droit, le notebook s'arrête avant d'écrire et le dit.

Ne jamais lancer `full` ou `rechunk` avec `parse_filter` : `chunks` serait réécrit avec les seuls documents filtrés et l'index perdrait le reste (`utils/parsing_pipeline/output_steps.py`).

Retour arrière d'une table : `CREATE OR REPLACE TABLE <table> DEEP CLONE <table>_before_merge`.
