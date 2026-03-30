"""Meeting-notes source — fetches unprocessed pages from the Meeting Notes DB.

Provides ``SingleSource`` which queries a single shared Notion database
with a configurable buffer delay and converts page blocks to plain text.
"""
from __future__ import annotations
