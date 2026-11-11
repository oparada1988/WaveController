"""
PipeWire JSON Parser & Utility Helper
======================================
Resilient pw-dump execution and multi-document JSON streaming parser.
Safely handles concatenated arrays, trailing extra data, and malformed PipeWire outputs.
"""

import json
import subprocess
from typing import List, Dict, Any


def safe_pw_dump(timeout: float = 3.0) -> List[Dict[str, Any]]:
    """Executes pw-dump and parses the JSON array, gracefully handling trailing extra data or multiple documents."""
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

        return data
    except Exception:
        return []
