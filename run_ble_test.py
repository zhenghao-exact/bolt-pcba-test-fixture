#!/usr/bin/env python3
"""
Standalone BLE test script for Bolt devices.

This script performs a BLE RSSI scan for a Bolt device by:
1. Restarting the bluetooth service
2. Optionally removing a cached bluetooth device
3. Scanning for the Bolt BLE device and collecting RSSI samples

Usage:
    python run_ble_test.py [bolt_id]
    
If bolt_id is not provided, the script will prompt for it.
"""

import sys
import time
import subprocess
import argparse

import bolt_control


def restart_bluetooth_service() -> bool:
    """
    Restart the bluetooth service using sudo.
    
    Returns True if successful, False otherwise.
    """
    print("BLE test: restarting bluetooth service...")
    process = None
    try:
        # Use sudo -S to read password from stdin
        process = subprocess.Popen(
            ["sudo", "-S", "systemctl", "restart", "bluetooth"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(input="123456\n", timeout=10.0)
        if process.returncode == 0:
            print("BLE test: bluetooth service restarted successfully")
            # Give bluetooth a moment to fully restart
            time.sleep(2.0)
            return True
        else:
            print(f"BLE test: bluetooth restart failed: {stderr}")
            return False
    except subprocess.TimeoutExpired:
        print("BLE test: bluetooth restart timed out")
        if process:
            process.kill()
        return False
    except Exception as exc:
        print(f"BLE test: error restarting bluetooth: {exc}")
        if process:
            try:
                process.kill()
            except Exception:
                pass
        return False


def remove_cached_bolt_devices(bolt_id: str) -> bool:
    """
    Remove any cached bluetooth devices whose name matches Bolt_<bolt_id>.

    BlueZ caches discovered devices in /var/lib/bluetooth/.../cache. Once a
    device has been seen, the controller's discovery filter can suppress
    further advertisement callbacks for it ("device already known"). Wiping
    the cache entry before each scan forces BlueZ to treat the next sighting
    as fresh.

    We parse `bluetoothctl devices` to find every cached device whose name
    contains Bolt_<bolt_id> and run `bluetoothctl remove <MAC>` on each. We
    do NOT hard-code a MAC because every Bolt board has its own address.

    Returns True if no errors were encountered (including the case where
    nothing matched), False otherwise.
    """
    target_name = f"Bolt_{bolt_id}"
    print(f"BLE test: clearing cached devices matching '{target_name}'...")
    try:
        list_proc = subprocess.run(
            ["bluetoothctl", "devices"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except subprocess.TimeoutExpired:
        print("BLE test: 'bluetoothctl devices' timed out")
        return False
    except Exception as exc:
        print(f"BLE test: error listing bluetooth devices: {exc}")
        return False

    macs_to_remove = []
    for line in list_proc.stdout.splitlines():
        # Format: "Device AA:BB:CC:DD:EE:FF Bolt_30000080"
        parts = line.strip().split(maxsplit=2)
        if len(parts) >= 3 and parts[0] == "Device" and target_name in parts[2]:
            macs_to_remove.append((parts[1], parts[2]))

    if not macs_to_remove:
        print(f"BLE test: no cached devices match '{target_name}'")
        return True

    ok = True
    for mac, name in macs_to_remove:
        print(f"BLE test: removing cached device {name} ({mac})")
        try:
            rm_proc = subprocess.run(
                ["bluetoothctl", "remove", mac],
                capture_output=True,
                text=True,
                timeout=10.0,
            )
            if rm_proc.returncode != 0:
                print(f"BLE test: remove {mac} failed: {rm_proc.stderr.strip()}")
                ok = False
        except subprocess.TimeoutExpired:
            print(f"BLE test: remove {mac} timed out")
            ok = False
        except Exception as exc:
            print(f"BLE test: error removing {mac}: {exc}")
            ok = False
    return ok


def run_ble_test(bolt_id: str, min_samples: int = 3, timeout_s: float = 30.0) -> bool:
    """
    Run the BLE RSSI scan for a Bolt device.
    
    This function collects RSSI from advertisement packets only, without connecting
    to the device. Connection-based RSSI collection (via GATT notifications) is not
    used in this standalone test.
    
    Args:
        bolt_id: The Bolt device ID to scan for (e.g., '30000080')
        min_samples: Minimum number of RSSI samples to collect
        timeout_s: Timeout in seconds for the BLE scan
    
    Returns True if the test passed, False otherwise.
    """
    if not bolt_id:
        print("Error: bolt_id is required")
        return False

    # Perform BLE RSSI scan from advertisements only (no connection)
    print(f"BLE test: scanning for Bolt_{bolt_id} (advertisement RSSI only, no connection)...")
    ok, median_rssi = bolt_control.scan_ble_advertisement_rssi(bolt_id, min_samples=min_samples, timeout_s=timeout_s)
    
    if ok:
        print(f"BLE test: PASSED - Median RSSI: {median_rssi} dBm")
    else:
        print(f"BLE test: FAILED - Could not collect sufficient RSSI samples")
    
    return ok


def main():
    """Main entry point for the standalone BLE test script."""
    parser = argparse.ArgumentParser(
        description="Run BLE test for a Bolt device",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_ble_test.py 30000080
  python run_ble_test.py --bolt-id 30000080 --min-samples 5 --timeout 45
        """
    )
    parser.add_argument(
        "bolt_id",
        nargs="?",
        help="Bolt device ID to scan for (e.g., '30000080')"
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=3,
        help="Minimum number of RSSI samples to collect (default: 3)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Timeout in seconds for BLE scan (default: 30.0)"
    )
    parser.add_argument(
        "--skip-restart",
        action="store_true",
        help="Skip bluetooth service restart"
    )
    parser.add_argument(
        "--skip-remove",
        action="store_true",
        help="Skip bluetooth device removal"
    )
    
    args = parser.parse_args()
    
    # Get bolt_id from command line or prompt
    bolt_id = args.bolt_id
    if not bolt_id:
        bolt_id = input("Enter Bolt ID (e.g., 30000080): ").strip()
        if not bolt_id:
            print("Error: Bolt ID is required")
            sys.exit(1)
    
    # Run the test
    if not args.skip_restart:
        restart_bluetooth_service()

    if not args.skip_remove:
        remove_cached_bolt_devices(bolt_id)

    success = run_ble_test(bolt_id, min_samples=args.min_samples, timeout_s=args.timeout)
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

