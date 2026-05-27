"""Diagnostic: probe how to rename a select option via Notion's API.

Tries 4 variants, refetches after each:
  --variant data_sources_with_id : the current macro_block_sync path
  --variant data_sources_no_color: same but omits the color field
  --variant databases             : the legacy databases.update endpoint
  --variant databases_no_color    : legacy endpoint without the color field

Reverts the test rename if it took effect.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from notion_client import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.notion_client_wrapper import NotionClientWrapper  # noqa: E402

JACOB = "35083e67-e2e7-80b0-9d8b-c213e9b161f3"
TARGET_ID = "BlAY"
TEST_NAME = "DIAG TEST RENAME"


def _print(label: str, opts: list[dict]) -> None:
    print(f"\n=== {label} ===")
    for o in opts:
        print(f"  id={o.get('id')!r:>14}  name={o.get('name')!r:60}  color={o.get('color')!r}")


def _retrieve_options(notion: NotionClientWrapper) -> list[dict]:
    ds = notion.retrieve_data_source(JACOB)
    return (
        ds.get("properties", {}).get("Macro Work Block", {}).get("select", {}).get("options", [])
    ) or []


def _build_options(opts: list[dict], target_name: str, with_color: bool) -> list[dict]:
    new = []
    for o in opts:
        entry = {"id": o["id"], "name": o["name"]}
        if with_color and o.get("color"):
            entry["color"] = o["color"]
        if o["id"] == TARGET_ID:
            entry["name"] = target_name
        new.append(entry)
    return new


def _run(notion: NotionClientWrapper, raw_client: Client, variant: str) -> bool:
    before = _retrieve_options(notion)
    _print(f"BEFORE  ({variant})", before)

    with_color = "no_color" not in variant
    options = _build_options(before, TEST_NAME, with_color)
    properties = {"Macro Work Block": {"select": {"options": options}}}

    if variant.startswith("data_sources"):
        ds_id = notion._resolve_data_source_id(JACOB)
        resp = raw_client.data_sources.update(data_source_id=ds_id, properties=properties)
    elif variant.startswith("databases"):
        # Legacy endpoint
        resp = raw_client.databases.update(database_id=JACOB, properties=properties)
    else:
        raise ValueError(f"unknown variant: {variant}")

    after_resp = (
        (resp.get("properties") or {}).get("Macro Work Block", {})
        .get("select", {}).get("options", [])
    )
    _print(f"RESPONSE ({variant})", after_resp)

    after_fetch = _retrieve_options(notion)
    _print(f"REFETCH  ({variant})", after_fetch)

    target = next((o for o in after_fetch if o.get("id") == TARGET_ID), None)
    took = target and target.get("name") == TEST_NAME
    if took:
        print(f"\n>>> {variant}: RENAME TOOK EFFECT — reverting...")
        # Revert
        original = next(o["name"] for o in before if o["id"] == TARGET_ID)
        revert_opts = _build_options(after_fetch, original, with_color)
        revert_props = {"Macro Work Block": {"select": {"options": revert_opts}}}
        if variant.startswith("data_sources"):
            ds_id = notion._resolve_data_source_id(JACOB)
            raw_client.data_sources.update(data_source_id=ds_id, properties=revert_props)
        else:
            raw_client.databases.update(database_id=JACOB, properties=revert_props)
        return True
    else:
        print(f"\n>>> {variant}: rename DID NOT take effect.")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--variant",
        choices=[
            "data_sources_with_color",
            "data_sources_no_color",
            "databases_with_color",
            "databases_no_color",
        ],
        required=True,
    )
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    raw = Client(auth=os.environ["NOTION_API_TOKEN"], notion_version="2026-03-11")
    notion = NotionClientWrapper(raw)

    _run(notion, raw, args.variant)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
