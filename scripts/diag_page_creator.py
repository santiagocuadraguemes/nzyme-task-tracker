"""One-off diagnostic: who created the Access Capital meeting page?

Replicates what `_resolve_delegated_user` sees: the page's created_by user id,
then retrieves that user to check type (person vs bot) and email. A bot/no-email
creator is what makes the GCal lookup fall back to GCAL_DELEGATED_USER_DEFAULT.
Read-only.
"""
from __future__ import annotations

import os
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["NOTION_API_TOKEN"]
PAGE_ID = "36e83e67-e2e7-8034-878e-f78e73ae96d7"
H = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2026-03-11",
}

page = requests.get(f"https://api.notion.com/v1/pages/{PAGE_ID}", headers=H).json()
cb = page.get("created_by", {})
cb_id = cb.get("id", "")
print(f"created_by id: {cb_id}")

user = requests.get(f"https://api.notion.com/v1/users/{cb_id}", headers=H).json()
print(f"  type:  {user.get('type')}")
print(f"  name:  {user.get('name')}")
email = (user.get("person") or {}).get("email")
print(f"  email: {email!r}   (bot/no-email -> falls back to GCAL default)")

# What the pipeline would impersonate:
default = os.getenv("GCAL_DELEGATED_USER_DEFAULT", "nzyme@kiboventures.com")
impersonate = (email or "").strip().lower() or default
print(f"\n=> pipeline would impersonate: {impersonate}")
