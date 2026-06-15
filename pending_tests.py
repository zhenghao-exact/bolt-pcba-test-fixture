"""Persistence for resumable Bolt test state (GUI fixture only).

When a board passes every test through analog calibration but the production
flash / sleep current stage does not complete — e.g. a PPK2 fixture issue, or a
sleep-current failure — the passed results + measurements are saved here keyed by
Bolt ID. When the operator re-scans the same board, bolt_fixture_main.py offers
to RESUME: skip the already-passed steps, re-flash production firmware and re-run
the sleep current test, then append a complete success row to the CSV.

State is one JSON file per Bolt ID under PENDING_DIR. A fully-passed board has its
state cleared.
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

PENDING_DIR = "/home/boltfixturepi/.bolt_pending_tests"

# Every one of these must have passed for a board to be eligible for the
# flash-prod + sleep-current resume shortcut (i.e. only the sleep stage is left).
RESUMABLE_PREREQS = (
    "qr_scan",
    "flash_test_fw",
    "usb_connection",
    "set_serial",
    "imu",
    "ble",
    "analog",
)


def _path(bolt_id: str) -> str:
    safe = str(bolt_id).replace("/", "_")
    return os.path.join(PENDING_DIR, f"{safe}.json")


def prereqs_passed(tests: Dict[str, Any]) -> bool:
    """True if every prerequisite test (everything up to the sleep stage) passed.

    A truthy value (including the 'sg' sentinel) counts as passed.
    """
    return all(tests.get(k) for k in RESUMABLE_PREREQS)


def sleep_stage_complete(tests: Dict[str, Any], measurements: Dict[str, Any]) -> bool:
    """True if the production flash + sleep current stage genuinely finished.

    "Finished" means a real measured sleep-current pass, or SG mode (where the
    stage is intentionally not required). An operator-skipped/deferred stage or a
    failed sleep current does NOT count as finished — those boards stay resumable
    so a re-scan can come back and complete them.
    """
    sc = tests.get("sleep_current")
    if not sc:
        return False  # failed or never run
    if sc == "sg":
        return True  # SG mode: stage intentionally skipped, board is complete
    # sleep_current is truthy: complete only if it was actually measured, not
    # marked skipped/deferred by the operator.
    if measurements.get("sleep_current_skipped") or measurements.get("sleep_current_ua") == "SKIPPED":
        return False
    return True


def save(bolt_id: str, tests: Dict[str, Any], measurements: Dict[str, Any]) -> None:
    """Persist the passed results + measurements for a resumable board."""
    if not bolt_id:
        return
    try:
        os.makedirs(PENDING_DIR, exist_ok=True)
        payload = {
            "bolt_id": bolt_id,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "tests": tests,
            "measurements": measurements,
        }
        with open(_path(bolt_id), "w") as f:
            json.dump(payload, f)
        print(f"Pending tests: saved resumable state for {bolt_id}")
    except Exception as exc:
        print(f"Pending tests: failed to save state for {bolt_id}: {exc}")


def load(bolt_id: str) -> Optional[Dict[str, Any]]:
    """Return the saved pending state for a Bolt ID, or None if there is none."""
    if not bolt_id:
        return None
    p = _path(bolt_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception as exc:
        print(f"Pending tests: failed to load state for {bolt_id}: {exc}")
        return None


def clear(bolt_id: str) -> None:
    """Delete any saved pending state for a Bolt ID (e.g. after a full pass)."""
    if not bolt_id:
        return
    p = _path(bolt_id)
    try:
        if os.path.exists(p):
            os.remove(p)
            print(f"Pending tests: cleared state for {bolt_id}")
    except Exception as exc:
        print(f"Pending tests: failed to clear state for {bolt_id}: {exc}")
