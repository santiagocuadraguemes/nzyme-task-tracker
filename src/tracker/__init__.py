"""Tracker package — writes ActionItems to the Macro Task Tracker database.

Contains the ``TaskTrackerWriter`` which converts normalised
``ActionItem`` instances into Notion page-creation payloads and writes
them to the target database via the Notion API.
"""
from __future__ import annotations
