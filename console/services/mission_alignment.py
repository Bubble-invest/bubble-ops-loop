"""Cached, read-only alignment snapshot using the console's manifest readers."""
from __future__ import annotations

import threading
import time

from console import settings
from console.services import dept_registry, github_reader
from scripts.lib.mission_alignment import alignment_report, default_intents_dir, read_intent_slugs

_lock = threading.Lock()
_cache_key = None
_cache_time = 0.0
_cache = None


def alignment_summary() -> dict:
    global _cache_key, _cache_time, _cache
    intents_dir = default_intents_dir()
    key = (settings.READ_FROM_DISK, str(intents_dir))
    with _lock:
        if _cache is not None and key == _cache_key and time.monotonic() - _cache_time < settings.GH_CACHE_TTL_SECONDS:
            return _cache
        manifests = []
        for dept in dept_registry.list_departments():
            if dept.slug in dept_registry.KNOWN_CONCIERGE_SLUGS:
                continue
            try:
                manifest = github_reader.load_dept_yaml(dept.slug)
            except (OSError, UnicodeError):
                manifest = None
            manifests.append((dept.slug, manifest))
        _cache = alignment_report(manifests, read_intent_slugs(intents_dir))
        _cache_key, _cache_time = key, time.monotonic()
        return _cache
