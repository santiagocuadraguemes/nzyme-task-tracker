"""Nzyme — AI-driven task extraction from Notion meeting notes.

Extracts action items from meeting notes using OpenAI function calling
and writes them to the Team Task Tracker database. A natural-language
playbook (stored as a Notion page) defines extraction rules, and the
tracker schema is read dynamically so Notion changes require no code updates.
"""
from __future__ import annotations
