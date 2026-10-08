"""Render target_config.env from utils/deploy/target_env.json for one target.

USAGE:
    python utils/deploy/render_target_config_env.py <dev|uat|uat-test|prod> [output_path]

Single source of truth for per-target app env overrides, shared by
deploy_qualibot.ps1 (local/manual deploys) and bitbucket-pipelines.yml (CI) --
avoids hardcoding the same values twice in two different scripting languages.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("target_env.json")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(f"Usage: {sys.argv[0]} <dev|uat|uat-test|prod> [output_path]")
    target = sys.argv[1]
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("target_config.env")

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    values = config.get(target)
    if values is None:
        available = [k for k in config if not k.startswith("_")]
        raise SystemExit(f"Unknown target '{target}'. Available: {available}")

    lines = [f"export {name}='{value}'" for name, value in values.items()]
    # newline="\n" forces LF regardless of OS — write_text()'s default text
    # mode translates "\n" to the platform separator, so running this on
    # Windows silently wrote CRLF, embedding a trailing \r in every value
    # and breaking any URL built from one ("Invalid non-printable ASCII
    # character in URL, '\r'").
    output_path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8", newline="\n")
    print(f"Wrote {len(lines)} var(s) to {output_path} for target '{target}'.")


if __name__ == "__main__":
    main()
