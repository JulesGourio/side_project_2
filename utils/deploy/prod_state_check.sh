#!/usr/bin/env bash
# PROD, read-only: what the bundle state tracks, what exists in Unity Catalog (prod_proj.qualibot and the former
# prod_landingzone.qualibot), the app, and the deploy plan. Changes nothing in Databricks or in the bundle state.
#
# USAGE (Bitbucket, deployment environment production): bash utils/deploy/prod_state_check.sh
set -uo pipefail

cd "$(dirname "$0")/../.."
P=job-runner-sa-prod
T=prod

bash utils/deploy/bitbucket_deploy.sh prod setup || exit 1
python3 utils/deploy/render_target_config_env.py prod || exit 1
databricks bundle validate -t $T -p $P >/dev/null || exit 1

echo "===== 1. Bundle state: schemas and volumes (key -> id, empty = not deployed)"
databricks bundle summary -t $T -p $P -o json | python3 -c "
import json, sys
res = json.load(sys.stdin).get('resources', {})
for kind in ('schemas', 'volumes', 'apps', 'postgres_projects'):
    for key, r in sorted(res.get(kind, {}).items()):
        print(f'{kind:18} {key:28} {r.get(\"id\") or \"(not deployed)\"}')
print('jobs deployed:', sum(1 for r in res.get('jobs', {}).values() if r.get('id')), '/', len(res.get('jobs', {})))
print('alerts deployed:', sum(1 for r in res.get('alerts', {}).values() if r.get('id')), '/', len(res.get('alerts', {})))
"

for schema in prod_proj.qualibot prod_landingzone.qualibot; do
    cat=${schema%%.*}; sch=${schema##*.}
    echo "===== 2. Unity Catalog: $schema"
    if databricks schemas get "$schema" -p $P >/dev/null 2>&1; then
        echo "-- volumes:"
        databricks volumes list "$cat" "$sch" -p $P -o json | python3 -c "
import json, sys
for v in json.load(sys.stdin) or []: print('  ', v['full_name'], v.get('volume_type'))"
        echo "-- tables:"
        databricks tables list "$cat" "$sch" -p $P -o json | python3 -c "
import json, sys
t = json.load(sys.stdin) or []
print('  ', len(t), 'tables:', ', '.join(sorted(x['name'] for x in t)))"
    else
        echo "   schema absent (or not readable by the PROD SP)"
    fi
done

echo "===== 3. docling_models volumes (files at the top level)"
for v in prod_proj/qualibot/docling_models prod_landingzone/qualibot/docling_models; do
    echo "-- /Volumes/$v:"
    databricks fs ls "dbfs:/Volumes/$v" -p $P 2>&1 | head -5
done

echo "===== 4. App"
databricks apps get qualibot -p $P -o json | python3 -c "
import json, sys
a = json.load(sys.stdin)
print('  ', a.get('name'), a.get('url'), (a.get('compute_status') or {}).get('state'), (a.get('app_status') or {}).get('state'))"

echo "===== 5. Deploy plan (nothing is applied)"
databricks bundle plan -t $T -p $P | tee /tmp/prod_plan.txt
echo "===== 6. Verdict"
if grep -Eiq '(delete|recreate)[[:space:]]+(resources\.)?(schemas|volumes)\.' /tmp/prod_plan.txt; then
    echo "DESTRUCTIVE: the plan deletes or recreates a schema or a volume. Run prod-state-fix, not deploy-prod-jobs."
else
    echo "OK: no schema or volume is deleted or recreated."
fi
