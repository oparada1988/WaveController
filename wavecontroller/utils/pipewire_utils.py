"""
PipeWire JSON Parser & Utility Helper
======================================
Resilient pw-dump execution and multi-document JSON streaming parser.
Safely handles concatenated arrays, trailing extra data, and malformed PipeWire outputs.
"""

import json
import subprocess
import threading
import time
from typing import List, Dict, Any

_pw_dump_lock = threading.Lock()
_pw_dump_cache: List[Dict[str, Any]] = []
_pw_dump_cache_ts: float = 0.0
_PW_DUMP_TTL_S: float = 0.35


def invalidate_pw_dump_cache() -> None:
    """Invalidates the short-lived safe_pw_dump cache after graph mutations."""
    global _pw_dump_cache_ts
    with _pw_dump_lock:
        _pw_dump_cache_ts = 0.0


def safe_pw_dump(timeout: float = 3.0, force_refresh: bool = False) -> List[Dict[str, Any]]:
    """Executes pw-dump and parses the JSON array, gracefully handling trailing extra data or multiple documents."""
    global _pw_dump_cache, _pw_dump_cache_ts
    now = time.monotonic()
    if not force_refresh:
        with _pw_dump_lock:
            if _pw_dump_cache and (now - _pw_dump_cache_ts) < _PW_DUMP_TTL_S:
                return _pw_dump_cache

    try:
        raw = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL, timeout=timeout).strip()
        if not raw:
            return []

        data = None
        # Fast path: single clean JSON document
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                data = None
        except Exception:
            pass

        # Resilient path: iterate and parse JSON documents/arrays (handles Extra data)
        if data is None:
            decoder = json.JSONDecoder()
            raw_len = len(raw)
            first_bracket = raw.find("[")
            if first_bracket != -1:
                idx = first_bracket
                all_objs = []
                while idx < raw_len:
                    while idx < raw_len and raw[idx].isspace():
                        idx += 1
                    if idx >= raw_len or raw[idx] != "[":
                        break
                    try:
                        obj, next_idx = decoder.raw_decode(raw, idx=idx)
                        if isinstance(obj, list):
                            all_objs.extend(obj)
                        idx = next_idx
                    except Exception:
                        break
                if all_objs:
                    data = all_objs

        # Fallback path: slice from first [ to last ]
        if data is None:
            first_bracket = raw.find("[")
            end_bracket = raw.rfind("]")
            if first_bracket != -1 and end_bracket > first_bracket:
                try:
                    obj = json.loads(raw[first_bracket:end_bracket + 1])
                    if isinstance(obj, list):
                        data = obj
                except Exception:
                    pass

        if not isinstance(data, list):
            return []

        # Sanitize object topology to prevent NoneType attribute errors
        for obj in data:
            if isinstance(obj, dict):
                if not isinstance(obj.get("info"), dict):
                    obj["info"] = {}
                if not isinstance(obj["info"].get("props"), dict):
                    obj["info"]["props"] = {}
                if not isinstance(obj["info"].get("params"), dict):
                    obj["info"]["params"] = {}

        with _pw_dump_lock:
            _pw_dump_cache = data
            _pw_dump_cache_ts = time.monotonic()

        return data
    except Exception:
        with _pw_dump_lock:
            if _pw_dump_cache and (time.monotonic() - _pw_dump_cache_ts) < 2.0:
                return _pw_dump_cache
        return []
