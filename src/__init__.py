"""Nzyme Macro Task Tracker — automated sync engine.

This package extracts action items from Notion meeting-notes databases
and writes them to a centralised Macro Task Tracker database.  It supports
two operating modes:

* **Single-source (Mode A):** one shared meeting-notes database.
* **Multi-source (Mode B):** a registry of per-person databases with
  heterogeneous schemas.
"""
from __future__ import annotations
