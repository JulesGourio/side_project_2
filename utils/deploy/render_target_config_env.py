"""Render target_config.env from utils/deploy/target_env.json for one target.

USAGE:
    python utils/deploy/render_target_config_env.py <dev|uat|prod> [output_path]

Writes the per-target app env overrides of target_env.json as shell exports.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("target_env.json")


import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("render_target_config_env")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(f"Usage: {sys.argv[0]} <dev|uat|prod> [output_path]")
    target = sys.argv[1]
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("target_config.env")

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    values = config.get(target)
    if values is None:
        available = [k for k in config if not k.startswith("_")]
        raise SystemExit(f"Unknown target '{target}'. Available: {available}")

    lines = [f"export {name}='{value}'" for name, value in values.items()]
    # Explicit LF: a CRLF newline would leave a trailing \r in every value.
    output_path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8", newline="\n")
    logger.info(f"Wrote {len(lines)} var(s) to {output_path} for target '{target}'.")


if __name__ == "__main__":
    main()
