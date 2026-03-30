"""Meeting-notes source implementations.

This package provides the adapters that fetch meeting-note pages from
Notion databases.  Two strategies are supported:

* ``SingleSource``  — reads from one shared database (Mode A).
* ``MultiSource``   — reads a registry of per-person databases and
  iterates over all active sources (Mode B).

Both strategies ultimately yield raw Notion page dicts that downstream
extraction modules consume.
"""
from __future__ import annotations
