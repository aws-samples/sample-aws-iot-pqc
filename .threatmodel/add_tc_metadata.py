#!/usr/bin/env python3
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Add Priority and STRIDE metadata to a threat-modeling MCP Threat Composer export.

The threat-modeling MCP server records a STRIDE category and a severity for
every threat, but its strict Threat Composer export (<base>.tc.json) writes an
empty "metadata" list, so Threat Composer shows no priority or STRIDE values.
The values are still present in the companion <base>.extended.json snapshot.

This script copies them into the .tc.json, using the metadata keys Threat
Composer reads:
  - "Priority": "High" | "Medium" | "Low"
  - "STRIDE":   list of letters, e.g. ["E"]

Severity is mapped to Threat Composer's three priority levels:
  Critical -> High, High -> High, Medium -> Medium, Low -> Low

Run it after every export_threat_model call, because each export overwrites
the .tc.json:

    python3 .threatmodel/add_tc_metadata.py
    python3 .threatmodel/add_tc_metadata.py --base pqc-iot-workshop-threat-model

It is idempotent: existing Priority/STRIDE entries are replaced, any other
metadata is kept. Threats are matched on numericId and the statements are
cross-checked; any mismatch aborts without writing.
"""
import argparse
import json
import os
import sys

STRIDE_LETTERS = {
    "Spoofing": "S",
    "Tampering": "T",
    "Repudiation": "R",
    "Information Disclosure": "I",
    "Denial of Service": "D",
    "Elevation of Privilege": "E",
}

PRIORITY_FOR_SEVERITY = {
    "Critical": "High",
    "High": "High",
    "Medium": "Medium",
    "Low": "Low",
}

MANAGED_KEYS = {"Priority", "STRIDE"}

# Compare this many leading characters of each statement. The .tc.json export
# may shorten long fields, so only the prefix is guaranteed to match.
STATEMENT_PREFIX = 80


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        sys.exit(f"[ERROR] File not found: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"[ERROR] {path} is not valid JSON: {exc}")


def build_metadata(ext_threat):
    """Return the Priority/STRIDE metadata entries for one extended threat."""
    entries = []
    severity = ext_threat.get("severity")
    if severity:
        priority = PRIORITY_FOR_SEVERITY.get(severity)
        if priority is None:
            raise ValueError(f"unknown severity '{severity}'")
        entries.append({"key": "Priority", "value": priority})
    category = ext_threat.get("category")
    if category:
        letter = STRIDE_LETTERS.get(category)
        if letter is None:
            raise ValueError(f"unknown STRIDE category '{category}'")
        entries.append({"key": "STRIDE", "value": [letter]})
    return entries


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--base",
        default="pqc-iot-workshop-threat-model",
        help="export base filename in this folder (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report the changes without writing"
    )
    args = parser.parse_args()

    tc_path = os.path.join(here, f"{args.base}.tc.json")
    ext_path = os.path.join(here, f"{args.base}.extended.json")
    tc = load_json(tc_path)
    ext = load_json(ext_path)

    ext_by_num = {t["numericId"]: t for t in ext.get("threats", [])}
    tc_threats = tc.get("threats", [])
    if len(ext_by_num) != len(tc_threats):
        sys.exit(
            f"[ERROR] Threat counts differ: {len(tc_threats)} in .tc.json, "
            f"{len(ext_by_num)} in .extended.json. Re-export both and retry."
        )

    changes = []
    for tc_threat in tc_threats:
        num = tc_threat.get("numericId")
        ext_threat = ext_by_num.get(num)
        if ext_threat is None:
            sys.exit(f"[ERROR] No extended threat with numericId {num}.")
        tc_stmt = (tc_threat.get("statement") or "")[:STATEMENT_PREFIX]
        ext_stmt = (ext_threat.get("statement") or "")[:STATEMENT_PREFIX]
        if tc_stmt != ext_stmt:
            sys.exit(
                f"[ERROR] Statement mismatch for numericId {num} "
                f"({ext_threat.get('id')}). The two files are from different "
                "exports; re-export and retry."
            )
        try:
            new_entries = build_metadata(ext_threat)
        except ValueError as exc:
            sys.exit(f"[ERROR] {ext_threat.get('id')}: {exc}")
        kept = [m for m in (tc_threat.get("metadata") or [])
                if m.get("key") not in MANAGED_KEYS]
        tc_threat["metadata"] = kept + new_entries
        changes.append((ext_threat.get("id"), ext_threat.get("severity"),
                        ext_threat.get("category"), new_entries))

    for tid, sev, cat, entries in changes:
        vals = ", ".join(f"{e['key']}={e['value']}" for e in entries)
        print(f"  {tid}: {sev} / {cat} -> {vals}")

    if args.dry_run:
        print(f"[DRY RUN] {len(changes)} threats would be updated in {tc_path}")
        return

    tmp_path = tc_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        # Match the exporter's layout (2-space indent, no trailing newline) so
        # the only diff is the added metadata.
        json.dump(tc, fh, indent=2, ensure_ascii=False)
    os.replace(tmp_path, tc_path)
    print(f"[OK] Added Priority and STRIDE to {len(changes)} threats in {tc_path}")


if __name__ == "__main__":
    main()
