#!/usr/bin/env python3
"""Bulk-import adversarial_mirror_pass из docs/backtests/*.md в detector_state.

Phase 0 Stage 3 follow-up: устраняет MANUAL_OVERRIDE_NEEDED для детекторов где
adversarial_mirror_pass уже задан в backtest .md frontmatter.

Action:
  - true / false → POST /api/bot/v2/lifecycle/adversarial_mirror
  - null → skip (требует human eval)
  - partial → skip + log warning (ручное решение нужно)
  - detector нет в detector_state → skip (legacy / disabled)

Usage:
  # Dry-run (default — печатает план, не пишет):
  ADMIN_API_KEY=xxx python scripts/bulk_set_adversarial_mirror.py

  # Apply:
  ADMIN_API_KEY=xxx API_BASE=http://127.0.0.1:8000 \
    python scripts/bulk_set_adversarial_mirror.py --apply

  # Custom base URL (для запуска с локали):
  ADMIN_API_KEY=xxx API_BASE=https://example.com \
    python scripts/bulk_set_adversarial_mirror.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

# Allow import from app.* без installed package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.backtest_metadata import list_available_backtests, parse_backtest_md  # noqa: E402


def _format_detector_state_url(api_base: str) -> str:
    return f"{api_base.rstrip('/')}/api/bot/v2/lifecycle/adversarial_mirror"


def _fetch_detectors_in_state(api_base: str, admin_key: str) -> set[str]:
    """Read detector_state names через /api/bot/v2/lifecycle/live_readiness response."""
    url = f"{api_base.rstrip('/')}/api/bot/v2/lifecycle/live_readiness"
    headers = {"Authorization": f"Bearer {admin_key}"}
    with httpx.Client(timeout=20) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return {d["detector"] for d in data.get("detectors", [])}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually call POST endpoint. Default: dry-run.",
    )
    parser.add_argument(
        "--note-prefix",
        default="bulk_import from docs/backtests",
        help="Prefix для notes поля. Полный note = prefix + adversarial_mirror_note из .md",
    )
    args = parser.parse_args()

    admin_key = os.environ.get("ADMIN_API_KEY")
    if not admin_key:
        print("ERROR: ADMIN_API_KEY env var required", file=sys.stderr)
        return 1

    api_base = os.environ.get("API_BASE", "http://127.0.0.1:8000")
    endpoint = _format_detector_state_url(api_base)

    # 1. List все backtests с frontmatter
    backtest_dets = list_available_backtests()
    print(f"Found {len(backtest_dets)} backtest .md files in docs/backtests/\n")

    # 2. Fetch live readiness → list of detectors в state
    print(f"Fetching detector_state via {api_base}/api/bot/v2/lifecycle/live_readiness ...")
    try:
        state_dets = _fetch_detectors_in_state(api_base, admin_key)
    except httpx.HTTPError as e:
        print(f"ERROR fetching detector_state: {e}", file=sys.stderr)
        return 1
    print(f"  {len(state_dets)} detectors в detector_state.\n")

    # 3. Classify per backtest
    plan_apply: list[tuple[str, bool, str | None]] = []
    skip_null: list[str] = []
    skip_partial: list[str] = []
    skip_missing_state: list[str] = []
    skip_no_field: list[str] = []

    for det in backtest_dets:
        meta = parse_backtest_md(det)
        if not meta:
            continue
        if "adversarial_mirror_pass" not in meta:
            skip_no_field.append(det)
            continue
        adv = meta["adversarial_mirror_pass"]
        note = meta.get("adversarial_mirror_note")

        # Skip detectors which don't exist in detector_state (e.g. D1 legacy disabled)
        if det not in state_dets:
            skip_missing_state.append(det)
            continue

        if adv is True or adv is False:
            plan_apply.append((det, adv, note))
        elif adv is None:
            skip_null.append(det)
        elif isinstance(adv, str) and adv.lower() == "partial":
            skip_partial.append(det)
        else:
            # Unexpected type — log
            print(f"  WARN: {det}: unexpected adversarial value {adv!r} ({type(adv).__name__}) — skip")
            skip_null.append(det)

    # 4. Report plan
    print("=== PLAN ===")
    print(f"APPLY ({len(plan_apply)} detectors):")
    for det, adv, note in plan_apply:
        note_short = (note[:60] + "...") if note and len(note) > 60 else (note or "")
        print(f"  {det:40} pass={str(adv):5}  {note_short}")

    if skip_null:
        print(f"\nSKIP null ({len(skip_null)} — нужна human evaluation):")
        for det in skip_null:
            print(f"  {det}")

    if skip_partial:
        print(f"\nSKIP partial ({len(skip_partial)} — ручное решение pass/fail):")
        for det in skip_partial:
            print(f"  {det}")

    if skip_missing_state:
        print(f"\nSKIP missing detector_state ({len(skip_missing_state)} — legacy/disabled):")
        for det in skip_missing_state:
            print(f"  {det}")

    if skip_no_field:
        print(f"\nSKIP no adversarial_mirror_pass field ({len(skip_no_field)}):")
        for det in skip_no_field:
            print(f"  {det}")

    if not args.apply:
        print(f"\nDry-run. Run with --apply to actually POST {len(plan_apply)} updates.")
        return 0

    # 5. Execute
    print(f"\n=== APPLYING {len(plan_apply)} updates ===")
    headers = {
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    succeeded = 0
    failed: list[tuple[str, str]] = []

    with httpx.Client(timeout=20) as client:
        for det, adv, note in plan_apply:
            full_note = args.note_prefix
            if note:
                full_note = f"{args.note_prefix}: {note}"
            payload = {"detector": det, "pass": adv, "notes": full_note}
            try:
                resp = client.post(endpoint, headers=headers, json=payload)
                resp.raise_for_status()
                body = resp.json()
                if body.get("ok"):
                    succeeded += 1
                    print(f"  OK   {det:40} pass={str(adv):5}")
                else:
                    failed.append((det, str(body)))
                    print(f"  FAIL {det:40} {body}")
            except httpx.HTTPError as e:
                failed.append((det, str(e)))
                print(f"  FAIL {det:40} {e}")

    print(f"\n=== RESULT: {succeeded}/{len(plan_apply)} applied ===")
    if failed:
        print(f"Failed: {len(failed)}")
        for det, err in failed:
            print(f"  {det}: {err}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
