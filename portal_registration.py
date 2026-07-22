#!/usr/bin/env python3
"""Register Bolt devices on the EXACT Portal (MBS) via the Device Registration API.

Used by the fixture to auto-register a Bolt temperature sensor at the end of a
passing test so its readings are visible on the Portal without a manual step.

The flow is the Bolt-specific two-call sequence from the Device Registration API
v5.0: POST /1/checkAvailableKeys then, if the key is free, POST /1/registerNewDevice.
Bolt values: type=26, batteryType=6, no secondaryKey. Registration is idempotent —
a key that already exists (checkAvailableKeys -> "Unavailable") is treated as success
so re-testing / resuming a board does not error.

Can also be run standalone:  python portal_registration.py <BOLT_SERIAL>
"""

import os
import sys
import json
import requests
from requests.exceptions import RequestException

CHECK_AVAILABLE_KEYS_URL = "https://exact.external.exacttechnology.com/1/checkAvailableKeys"
REGISTER_NEW_DEVICE_URL = "https://exact.external.exacttechnology.com/1/registerNewDevice"

# The EXACT Portal API key is NOT committed. It is provisioned per host as a
# one-line, git-ignored file next to this module (see .gitignore: *.txt), or via
# the EXACT_API_KEY environment variable (which takes precedence).
API_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal_api_key.txt")


def _load_api_key() -> str:
    """Resolve the EXACT Portal API key: EXACT_API_KEY env var, else the key file.

    Raises RuntimeError with an actionable message if neither is present, so a
    missing key surfaces as a best-effort registration failure (caught by the
    caller and logged) rather than a silent skip.
    """
    key = os.environ.get("EXACT_API_KEY")
    if key and key.strip():
        return key.strip()
    try:
        with open(API_KEY_FILE, "r") as f:
            key = f.read().strip()
    except OSError as e:
        raise RuntimeError(
            f"EXACT Portal API key not found: set EXACT_API_KEY or create {API_KEY_FILE} "
            f"(one line, git-ignored). Underlying error: {e}"
        )
    if not key:
        raise RuntimeError(f"EXACT Portal API key file is empty: {API_KEY_FILE}")
    return key


def _api_key_header() -> dict:
    return {"X-EXACT-APIKEY": _load_api_key()}

# Bolt device configuration.
PROJECT_ID = 716
DEVICE_TYPE = 26
BATTERY_TYPE = 6
INTERVAL_MINUTES = 15
LOG_INTERVAL_S = 900
TX_INTERVAL_S = 900
POWER_MODE = 0


def _post_json(url: str, headers: dict, payload: dict, timeout: float = 5.0) -> requests.Response:
    """POST JSON to the EXACT API with basic retry on network errors.

    Returns the requests.Response; raises RequestException on network-level errors
    after the retries are exhausted.
    """
    max_retries = 3
    last_exception = None

    for attempt in range(max_retries):
        try:
            return requests.post(url=url, headers=headers, json=payload, timeout=timeout)
        except RequestException as e:
            last_exception = e
            if attempt < max_retries - 1:
                continue
            raise last_exception

    raise last_exception


def check_key_availability(serial: str) -> str:
    """Check if a Bolt serial key is available on the EXACT Portal.

    Returns "Available" or "Unavailable"; raises RuntimeError on API errors.
    """
    payload = {
        "serial_keys": [serial]
    }

    try:
        response = _post_json(CHECK_AVAILABLE_KEYS_URL, _api_key_header(), payload)

        if response.status_code == 200:
            try:
                body = response.json()
                status = body.get(serial)
                if status in ["Available", "Unavailable"]:
                    return status
                else:
                    raise RuntimeError(
                        f"Unexpected response format for serial {serial}. "
                        f"Expected 'Available' or 'Unavailable', got: {status}"
                    )
            except (json.JSONDecodeError, AttributeError) as e:
                raise RuntimeError(
                    f"Failed to parse response JSON for serial {serial}: {e}. "
                    f"Response body: {response.text}"
                )
        elif response.status_code == 400:
            try:
                error_body = response.json()
                error_code = error_body.get("error_code", "UNKNOWN")
                error_message = error_body.get("error_message", "No error message provided")
                errors = error_body.get("errors", {})
                raise RuntimeError(
                    f"API returned 400 Bad Request for serial {serial}. "
                    f"error_code: {error_code}, error_message: {error_message}, errors: {errors}"
                )
            except json.JSONDecodeError:
                raise RuntimeError(
                    f"API returned 400 Bad Request for serial {serial}. "
                    f"Response body: {response.text}"
                )
        else:
            raise RuntimeError(
                f"API returned status {response.status_code} for serial {serial}. "
                f"Response body: {response.text}"
            )

    except RequestException as e:
        raise RuntimeError(f"Network error while calling EXACT API for serial {serial}: {e}")


def register_bolt_device(serial: str) -> dict:
    """Register a Bolt device on the EXACT Portal.

    Returns the response JSON dict (with 'success' and 'id'); raises RuntimeError on
    API errors.
    """
    payload = {
        "projectId": str(PROJECT_ID),
        "name": serial,
        "primaryKey": serial,
        "type": str(DEVICE_TYPE),
        "batteryType": str(BATTERY_TYPE),
        "interval": str(INTERVAL_MINUTES),
        "logInterval": str(LOG_INTERVAL_S),
        "txInterval": str(TX_INTERVAL_S),
        "powerMode": str(POWER_MODE),
    }

    try:
        response = _post_json(REGISTER_NEW_DEVICE_URL, _api_key_header(), payload)

        if response.status_code == 200:
            try:
                body = response.json()
                if body.get("success") is True:
                    return body
                else:
                    raise RuntimeError(
                        f"Registration response for serial {serial} did not indicate success. "
                        f"Response: {body}"
                    )
            except (json.JSONDecodeError, AttributeError) as e:
                raise RuntimeError(
                    f"Failed to parse registration response JSON for serial {serial}: {e}. "
                    f"Response body: {response.text}"
                )
        elif response.status_code == 400:
            try:
                error_body = response.json()
                error_code = error_body.get("error_code", "UNKNOWN")
                error_message = error_body.get("error_message", "No error message provided")
                errors = error_body.get("errors", {})
                raise RuntimeError(
                    f"API returned 400 Bad Request for serial {serial}. "
                    f"error_code: {error_code}, error_message: {error_message}, errors: {errors}"
                )
            except json.JSONDecodeError:
                raise RuntimeError(
                    f"API returned 400 Bad Request for serial {serial}. "
                    f"Response body: {response.text}"
                )
        else:
            raise RuntimeError(
                f"API returned status {response.status_code} for serial {serial}. "
                f"Response body: {response.text}"
            )

    except RequestException as e:
        raise RuntimeError(f"Network error while calling EXACT API for serial {serial}: {e}")


def ensure_bolt_on_portal(serial: str) -> tuple:
    """Ensure a Bolt device exists on the EXACT Portal.

    Checks availability and registers the device if the key is free. A key that is
    already taken is treated as success (idempotent — supports re-test / resume).

    Returns (ok: bool, message: str). The caller logs the message; a False result
    must never fail the board (registration is a best-effort side effect).
    """
    try:
        status = check_key_availability(serial)

        if status == "Unavailable":
            return True, f"{serial}: already registered on EXACT Portal."
        elif status == "Available":
            response = register_bolt_device(serial)
            device_id = response.get("id", "unknown")
            return True, f"{serial}: registered on EXACT Portal with id {device_id}."
        else:
            return False, f"{serial}: unexpected status '{status}' from availability check."

    except RuntimeError as e:
        return False, f"{serial}: registration failed: {e}"
    except Exception as e:
        return False, f"{serial}: unexpected error during registration: {e}"


def main(argv: list) -> int:
    if len(argv) != 1:
        print("Usage: python portal_registration.py <BOLT_SERIAL>")
        return 1

    serial = argv[0].strip()
    if not serial:
        print("Error: BOLT_SERIAL cannot be empty.")
        return 1

    ok, message = ensure_bolt_on_portal(serial)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
