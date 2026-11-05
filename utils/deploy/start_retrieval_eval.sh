#!/usr/bin/env bash
# Starts the retrieval evaluation of the version just deployed, without waiting for it: the job measures the deployed
# configuration on the golden set (preset deploye) and stops at once when its agent version was already measured on
# that golden set (only_if_new). The deploy never fails here; the job mails its own failures.
#
# USAGE: bash utils/deploy/start_retrieval_eval.sh <databricks CLI profile>
set -uo pipefail

PROFILE="${1:?usage: start_retrieval_eval.sh <profile>}"
JOB_NAME="Z_4_Qualibot_Retrieval_Eval"

job_id=$(databricks jobs list --name "$JOB_NAME" -o json -p "$PROFILE" | python3 -c '
import json, sys
jobs = json.load(sys.stdin)
jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
print(jobs[0]["job_id"] if jobs else "")
') || job_id=""

if [ -z "$job_id" ]; then
    echo "WARNING: job $JOB_NAME not found, retrieval evaluation not started"
    exit 0
fi
if databricks jobs run-now --no-wait -p "$PROFILE" \
        --json "{\"job_id\": $job_id, \"job_parameters\": {\"preset\": \"deploye\", \"only_if_new\": \"true\"}}"; then
    echo "Retrieval evaluation started (job $job_id), not awaited"
else
    echo "WARNING: retrieval evaluation not started (job $job_id)"
fi
exit 0
