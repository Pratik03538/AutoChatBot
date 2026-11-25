"""Safely toggle live WhatsApp auto-replies in the ignored local contacts.json.

The live-enable path is deliberately restricted to exactly one enabled contact.
It makes a timestamped backup before changing any settings.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(__file__).resolve().parent / "contacts.json"
DEFAULT_TONE = "casual, natural and understated"


def update_live_mode(config_path: Path, contact_name: str | None, enable_live: bool) -> Path:
    config_path = Path(config_path)
    if not config_path.is_file():
        raise FileNotFoundError(
            f"{config_path} nahi mila. Pehle contacts.json create/configure karein."
        )

    try:
        config: Any = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"contacts.json JSON invalid hai: {error}") from error

    if not isinstance(config, dict) or not isinstance(config.get("contacts"), list):
        raise ValueError("contacts.json mein top-level object aur 'contacts' list zaroori hai.")

    contacts = config["contacts"]
    if any(not isinstance(item, dict) for item in contacts):
        raise ValueError("contacts list ka har item JSON object hona chahiye.")

    if enable_live:
        enabled = [item for item in contacts if item.get("enabled") is True]
        matches = [
            item for item in enabled
            if str(item.get("name") or "").strip() == str(contact_name or "").strip()
        ]
        if len(enabled) != 1 or len(matches) != 1:
            enabled_names = [str(item.get("name") or "(unnamed)") for item in enabled]
            raise ValueError(
                "Safety stop: live mode sirf tab enable hoga jab exactly one enabled contact "
                f"enabled ho aur uska name {contact_name!r} ho. Enabled contacts: {enabled_names}"
            )

        target = matches[0]
        for item in contacts:
            item["auto_send"] = item is target
        target["tone"] = DEFAULT_TONE
        config["dry_run"] = False
    else:
        config["dry_run"] = True
        for item in contacts:
            item["auto_send"] = False

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = config_path.with_name(f"{config_path.name}.backup-{stamp}")
    shutil.copy2(config_path, backup_path)

    temporary_path = config_path.with_name(f"{config_path.name}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, config_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enable live auto-replies for one explicitly selected WhatsApp contact."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--enable-live",
        action="store_true",
        help="Disable dry-run and allow auto-send for exactly one enabled contact.",
    )
    mode.add_argument(
        "--disable-live",
        action="store_true",
        help="Restore dry-run and disable auto-send for every contact.",
    )
    parser.add_argument(
        "--contact-name",
        help="Exact local contacts.json name; required with --enable-live.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to contacts.json (default: project folder).",
    )
    args = parser.parse_args()

    if args.enable_live and not args.contact_name:
        parser.error("--contact-name is required with --enable-live")

    try:
        backup = update_live_mode(
            args.config,
            args.contact_name,
            enable_live=args.enable_live,
        )
    except (OSError, ValueError) as error:
        print(f"[NOT CHANGED] {error}")
        return 1

    if args.enable_live:
        print("[LIVE ENABLED] dry_run=false; auto_send=true for exactly one enabled contact.")
        print("[STYLE] Target tone set to casual, natural and understated.")
    else:
        print("[SAFE MODE RESTORED] dry_run=true; auto_send=false for every contact.")
    print(f"[BACKUP] Previous config saved to: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
