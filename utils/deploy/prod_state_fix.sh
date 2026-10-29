#!/usr/bin/env bash
# PROD, one-off: detach the bundle state from the pre-refactor objects of prod_landingzone.qualibot
# (schema qualibot_schema, volumes doc_compare, docling_models, images, test) and bind the prod_proj.qualibot
# objects that already exist. State only: nothing is deleted or created in Databricks, the old schema,
# its tables and its volumes stay as they are. Ends with a plan; fails if it still deletes or recreates a schema or a volume.
#
# USAGE (Bitbucket, deployment environment production): bash utils/deploy/prod_state_fix.sh
set -euo pipefail

cd "$(dirname "$0")/../.."
P=job-runner-sa-prod
T=prod

bash utils/deploy/bitbucket_deploy.sh prod setup
python3 utils/deploy/render_target_config_env.py prod

# unbind needs the key in the configuration: the two removed keys are declared again for this run only
# (file written in the Bitbucket clone, never committed).
cat > resources/zz_prod_state_fix_tmp.yml <<'EOF'
resources:
  schemas:
    qualibot_schema:
      catalog_name: prod_landingzone
      name: qualibot
  volumes:
    qualibot_test:
      catalog_name: prod_landingzone
      schema_name: qualibot
      name: test
      volume_type: MANAGED
EOF

databricks bundle validate -t $T -p $P

# Keys tracked by the state, with their current id (empty when not deployed).
state_id() {
    databricks bundle summary -t $T -p $P -o json | python3 -c "
import json, sys
kind, key = sys.argv[1], sys.argv[2]
r = json.load(sys.stdin).get('resources', {}).get(kind, {}).get(key, {})
print(r.get('id') or '')" "$1" "$2"
}

for key in qualibot_schema:schemas qualibot_test:volumes qualibot_doc_compare:volumes qualibot_docling_models:volumes qualibot_images:volumes; do
    name="${key%%:*}"; kind="${key##*:}"
    id="$(state_id "$kind" "$name")"
    if [ -n "$id" ] && [[ "$id" != prod_proj.* ]]; then
        echo ">> unbind $name (state points to $id, left untouched in Databricks)"
        databricks bundle deployment unbind "$name" -t $T -p $P
    else
        echo ">> $name: nothing to unbind (state id: '${id}')"
    fi
done

rm -f resources/zz_prod_state_fix_tmp.yml
databricks bundle validate -t $T -p $P

# Bind the prod_proj.qualibot objects that exist and are not in the state yet. The missing ones are created by the next deploy.
bind_if_exists() {
    kind="$1"; key="$2"; full="$3"
    id="$(state_id "$kind" "$key")"
    if [ -n "$id" ]; then
        echo ">> $key already bound to $id"
        return
    fi
    if [ "$kind" = schemas ]; then
        exists_cmd=(databricks schemas get "$full" -p $P)
    else
        exists_cmd=(databricks volumes read "$full" -p $P)
    fi
    if "${exists_cmd[@]}" >/dev/null 2>&1; then
        echo ">> bind $key -> $full"
        databricks bundle deployment bind "$key" "$full" -t $T -p $P --auto-approve
    else
        echo ">> $full does not exist: the next deploy creates it"
    fi
}

bind_if_exists schemas qualibot_proj_schema prod_proj.qualibot
bind_if_exists volumes qualibot_doc_compare prod_proj.qualibot.doc_compare
bind_if_exists volumes qualibot_docling_models prod_proj.qualibot.docling_models
bind_if_exists volumes qualibot_images prod_proj.qualibot.images
bind_if_exists volumes qualibot_staging prod_proj.qualibot.staging

echo ">> plan after the fix"
databricks bundle plan -t $T -p $P | tee /tmp/prod_plan.txt
if grep -Eiq '(delete|recreate)[[:space:]]+(resources\.)?(schemas|volumes)\.' /tmp/prod_plan.txt; then
    echo "ERROR: the plan still deletes or recreates a schema or a volume (see above). Do not deploy." >&2
    exit 1
fi
echo "OK: no schema or volume is deleted or recreated. deploy-prod-jobs can run."
