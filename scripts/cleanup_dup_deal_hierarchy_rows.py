"""ONE-OFF cleanup: archive the duplicate **Hierarchy DB rows** that
``deal_hierarchy_sync`` created on its first (buggy) live run on 2026-06-02.

The writer keyed on the new ``Deal ID`` property, found no existing row carrying
one, and CREATED a second copy of 13 deals already hand-curated in the Hierarchy
DB. This script archives (Notion trash — reversible) ONLY those 13 rows, each
verified by a non-empty ``Deal ID`` so a hand-made row can never be touched.

It deliberately does NOT touch the ``[DETAILS INSIDE]`` Tracker nodes — per the
project's hard rule, only ``tracker_applier_sync`` may remove those. After this
script runs, run:

    python -m src.main --sync-hierarchy --sub-sync canonical_mirror_sync --sub-sync tracker_applier_sync

canonical_mirror_sync will tombstone the 13 now-missing rows and
tracker_applier_sync will archive their Tracker nodes the sanctioned way.

Run:  ../venv/Scripts/python -m scripts.cleanup_dup_deal_hierarchy_rows
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from notion_client import Client

# (deal name, hierarchy page id I created) — all carry a Deal ID (= mine).
TARGETS: list[tuple[str, str]] = [
    ("Azenea",                 "37383e67-e2e7-8103-84fd-f4f968c830e4"),
    ("Civislend",              "37383e67-e2e7-81c2-8775-da3446d79c61"),
    ("Grupo Moneris",          "37383e67-e2e7-81b7-bb03-e23c5f7b1301"),
    ("Grupo Your",             "37383e67-e2e7-817c-8cf1-f1d4f10a1417"),
    ("Project Lavare",         "37383e67-e2e7-819f-a35b-e2468f09c8db"),
    ("Prosenor",               "37383e67-e2e7-811e-aeca-c93cb40ef519"),
    ("RB Soluciones",          "37383e67-e2e7-81f4-a7cc-f189c5d4b7ef"),
    ("SEG - Project Keystone", "37383e67-e2e7-8143-95a3-e5df1a8e5142"),
    ("Sertyf",                 "37383e67-e2e7-818b-87ef-f78bf0b14580"),
    ("White Vega",             "37383e67-e2e7-8149-937f-fde1c4095520"),
    ("Integra Ambiental",      "37383e67-e2e7-81df-96a5-e868c7d52ed2"),
    ("Kuma Care",              "37383e67-e2e7-812e-b7f5-fb5e93e38297"),
    ("Project Poseidon",       "37383e67-e2e7-817d-95c3-e10cdf405c8e"),
]


def _plain(rich: list[dict]) -> str:
    return "".join(x.get("plain_text", "") for x in (rich or [])).strip()


def main() -> None:
    load_dotenv()
    client = Client(auth=os.environ["NOTION_API_TOKEN"], notion_version="2026-03-11")

    archived = 0
    for name, hier_page in TARGETS:
        page = client.pages.retrieve(page_id=hier_page)
        if page.get("archived") or page.get("in_trash"):
            print(f"SKIP {name!r}: already archived")
            continue
        deal_id = _plain(page.get("properties", {}).get("Deal ID", {}).get("rich_text"))
        if not deal_id:
            print(f"REFUSE {name!r} ({hier_page}): empty Deal ID — NOT a sync-created row, skipping")
            continue
        # API 2026-03-11 trashes via `in_trash` (not the legacy `archived`).
        client.request(path=f"pages/{hier_page}", method="PATCH", body={"in_trash": True})
        archived += 1
        print(f"archived {name!r} ({hier_page}) deal_id={deal_id}")

    print(f"\nArchived {archived} duplicate Hierarchy row(s) (expected 13).")
    print("Next: run canonical_mirror_sync + tracker_applier_sync to retire their Tracker nodes.")


if __name__ == "__main__":
    main()
