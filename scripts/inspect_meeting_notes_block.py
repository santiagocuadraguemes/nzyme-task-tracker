"""Diagnostic: dump the raw meeting_notes block payload for a given page."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from notion_client import Client


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    page_id = sys.argv[1] if len(sys.argv) > 1 else "34483e67e2e780cbad6eea99052e0135"
    c = Client(auth=os.environ["NOTION_API_TOKEN"], notion_version="2026-03-11")
    blocks = c.blocks.children.list(block_id=page_id).get("results", [])
    mn = next((b for b in blocks if b.get("type") == "meeting_notes"), None)
    if mn is None:
        print(f"no meeting_notes block on {page_id}", file=sys.stderr)
        return 1
    payload = mn.get("meeting_notes", {})
    trim = {k: v for k, v in payload.items() if k != "children"}
    print(json.dumps(trim, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
