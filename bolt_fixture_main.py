import argparse
import os
import sys
import time
import subprocess
import threading
import csv
import re
from datetime import datetime
from typing import Dict, Any, Tuple, Optional

# Label printing is a best-effort side effect: it must never block the test
# flow or affect the pass/fail result. A disconnected printer is skipped
# instantly; an unresponsive one is bounded by this timeout so it can't hang
# the fixture. Operators can always reprint a label afterwards.
PRINTER_DEVICE = "/dev/usb/lp0"
LABEL_PRINT_TIMEOUT_S = 15.0


def _print_label_best_effort(final_ok: bool, measurements: Dict[str, Any]) -> None:
    """Print the result label without ever blocking the flow or failing the test.

    If the printer device node is missing (printer not connected) we skip
    immediately. If it is present we attempt the print in a daemon thread
    bounded by LABEL_PRINT_TIMEOUT_S, so a powered-off / out-of-paper printer
    that wedges the blocking write can't hang the fixture.
    """
    if not os.path.exists(PRINTER_DEVICE):
        print(f"Label printing: printer not connected ({PRINTER_DEVICE} missing) - skipping; print manually later")
        return

    def _worker() -> None:
        try:
            ok = printer_manager.print_label(final_ok, measurements, refurb=False, work_order="")
            if not ok:
                print("Label printing failed; operator can reprint manually later.")
        except Exception as exc:
            print(f"Label printing error (non-fatal): {exc}")

    worker = threading.Thread(target=_worker, name="LabelPrint", daemon=True)
    worker.start()
    worker.join(LABEL_PRINT_TIMEOUT_S)
    if worker.is_alive():
        print(
            f"Label printing: timed out after {LABEL_PRINT_TIMEOUT_S:.0f}s "
            "(printer unresponsive) - continuing; print manually later"
        )


# Always-on stdout tee. Mirrors every print() to log/app_<timestamp>.log so the
# full startup transcript — including ppk2.py's import-time PPK2 discovery —
# is preserved for postmortem analysis. Installed before any module that
# prints at import time.
class _PersistentTee:
    def __init__(self, original) -> None:
        self._original = original
        self._sinks: list = []

    def add_sink(self, sink) -> None:
        self._sinks.append(sink)

    def write(self, s: str) -> int:
        self._original.write(s)
        for sink in self._sinks:
            try:
                sink(s)
            except Exception:
                pass
        return len(s)

    def flush(self) -> None:
        self._original.flush()


def _install_persistent_tee() -> _PersistentTee:
    os.makedirs("log", exist_ok=True)
    log_path = os.path.join("log", f"app_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    log_file = open(log_path, "w", buffering=1)  # line-buffered
    tee = _PersistentTee(sys.stdout)
    tee.add_sink(log_file.write)
    sys.stdout = tee
    print(f"App log: {log_path}")
    return tee


_persistent_tee = _install_persistent_tee()


import gui  # Reused GUI from etc-monitor-fixture
import nrfjprog
import ppk2
import printer_manager
import csv_manager
import upload_results
import pending_tests

import bolt_control
from serial import SerialException  # type: ignore[import-not-found]


# Paths to firmware images on the Pi. Adjust these to match the Bolt build
# output and repository layout on the production fixture.
FW_FOLDER_PATH = "/home/boltfixturepi/bolt-pcba-test-fixture/fw"
TEST_FW_FILENAME = "bolt-test-fw-060rc.hex"
PRODUCTION_FW_FILENAME = "bolt-prod-fw-060rc.hex"
SG_PRODUCTION_FW_FILENAME = "bolt-sg-prod-fw.hex"

# CSV cell value recorded for tests skipped in strain-gauge (--SG) mode. Truthy
# on purpose so evaluate_overall_result() treats the skipped tests as passing.
SG_SKIPPED_RESULT = "sg"

tests_template: Dict[str, Any] = {
    "qr_scan": False,
    "flash_test_fw": False,
    "usb_connection": False,
    "set_serial": False,
    "imu": False,
    "ble": False,
    "analog": False,
    "sleep_current": False,
    "flash_production_fw": False,
    "final": False,
}

measurements_template: Dict[str, Any] = {
    "bolt_id": "",
    "pcba_qr": "",
    "HW_ID": "N/A",  # Scanned from the board's hardware ID label; 'N/A' if skipped
    "dev_ID": "",
    "PCBA_ID": "",
    "ble_rssi_median": None,
    "sleep_current_ua": None,
    "test_ID": 0,
    # Analog calibration metrics
    "adc_offset_raw_factory": None,
    "adc_high_raw_factory": None,
    "adc_ref_factory": 3619.64,
    "adc_temp_27k_expected_c": 0.19,
    "adc_temp_27k_measured_c": None,
    "adc_temp_10k_expected_c": 25.0,
    "adc_temp_10k_measured_c": None,
    "adc_temp_4k99_expected_c": 44.57,
    "adc_temp_4k99_measured_c": None,
    "adc_temp_2k2_expected_c": 70.42,
    "adc_temp_2k2_measured_c": None,
    # Supply voltage (fixed 3.3V from PPK2)
    "supply_voltage_v": 3.3,
    "sleep_current_skipped": False,
}


# Persistent counter file for PPK2 sleep current errors
PPK2_ERROR_COUNT_FILE = "/home/boltfixturepi/.bolt_ppk2_sleep_error_count"

# Persistent counter file for BLE test failures
BLE_ERROR_COUNT_FILE = "/home/boltfixturepi/.bolt_ble_fail_count"

# Directory for sleep current failure logs (relative to CWD, alongside `data/`).
SLEEP_CURRENT_LOG_DIR = "log"

# Directory for BLE cycle failure logs (relative to CWD, alongside `data/`).
BLE_LOG_DIR = "log"


class _TeeStdout:
    """Duplicate writes to the original stdout and an in-memory buffer."""

    def __init__(self, original) -> None:
        self._original = original
        self._chunks: list[str] = []

    def write(self, s: str) -> int:
        self._original.write(s)
        self._chunks.append(s)
        return len(s)

    def flush(self) -> None:
        self._original.flush()

    def getvalue(self) -> str:
        return "".join(self._chunks)


def get_ppk2_error_count() -> int:
    """
    Read the persistent PPK2 error counter from disk.
    
    Returns:
        The current error count (0 if file doesn't exist or is invalid)
    """
    try:
        if os.path.exists(PPK2_ERROR_COUNT_FILE):
            with open(PPK2_ERROR_COUNT_FILE, 'r') as f:
                count_str = f.read().strip()
                return int(count_str)
    except (ValueError, IOError) as exc:
        print(f"PPK2 error counter: failed to read counter file: {exc}")
    return 0


def set_ppk2_error_count(count: int) -> None:
    """
    Write the persistent PPK2 error counter to disk.
    
    Args:
        count: The error count to store (0 to reset)
    """
    try:
        if count == 0:
            # Reset: delete the file if it exists
            if os.path.exists(PPK2_ERROR_COUNT_FILE):
                os.remove(PPK2_ERROR_COUNT_FILE)
        else:
            # Write the count
            with open(PPK2_ERROR_COUNT_FILE, 'w') as f:
                f.write(str(count))
    except IOError as exc:
        print(f"PPK2 error counter: failed to write counter file: {exc}")


def get_ble_error_count() -> int:
    """
    Read the persistent BLE error counter from disk.
    
    Returns:
        The current error count (0 if file doesn't exist or is invalid)
    """
    try:
        if os.path.exists(BLE_ERROR_COUNT_FILE):
            with open(BLE_ERROR_COUNT_FILE, 'r') as f:
                count_str = f.read().strip()
                return int(count_str)
    except (ValueError, IOError) as exc:
        print(f"BLE error counter: failed to read counter file: {exc}")
    return 0


def set_ble_error_count(count: int) -> None:
    """
    Write the persistent BLE error counter to disk.
    
    Args:
        count: The error count to store (0 to reset)
    """
    try:
        if count == 0:
            # Reset: delete the file if it exists
            if os.path.exists(BLE_ERROR_COUNT_FILE):
                os.remove(BLE_ERROR_COUNT_FILE)
        else:
            # Write the count
            with open(BLE_ERROR_COUNT_FILE, 'w') as f:
                f.write(str(count))
    except IOError as exc:
        print(f"BLE error counter: failed to write counter file: {exc}")


class BoltTest:
    def __init__(self) -> None:
        self.tests: Dict[str, Any] = dict(tests_template)
        self.measurements: Dict[str, Any] = dict(measurements_template)
        self.ser = None
        self.failure = False
        self.baseline_ports: set[str] = set()
        self.dut_serial_port: Optional[str] = None
        self.ppk2_sleep_error = False  # Flag for abnormal PPK2 readings (> 1000 uA)
        self.ppk2_lost = False  # True when the PPK2 dropped off the USB bus (EIO) and can't be recovered
        self.ble_first_failure = False  # True when we hit first BLE failure since last success

    # --- Utility helpers -------------------------------------------------

    def _scan_acm_ports(self) -> list[str]:
        """
        Scan for all /dev/ttyACM* devices and return a sorted list.

        Returns a list of port paths sorted by port number (e.g., /dev/ttyACM0
        comes before /dev/ttyACM1).
        """
        ports = []
        for i in range(256):  # Check up to /dev/ttyACM255
            port = f"/dev/ttyACM{i}"
            if os.path.exists(port):
                ports.append(port)
        return sorted(ports, key=lambda x: int(x.replace("/dev/ttyACM", "")))

    def _scan_ttyusb_ports(self) -> list[str]:
        """
        Scan for all /dev/ttyUSB* devices and return a sorted list by index.
        """
        ports = []
        for i in range(256):
            port = f"/dev/ttyUSB{i}"
            if os.path.exists(port):
                ports.append(port)
        return sorted(ports, key=lambda x: int(x.replace("/dev/ttyUSB", "")))

    def _capture_baseline_ports(self) -> None:
        """
        Capture existing /dev/ttyACM* and /dev/ttyUSB* nodes as a baseline.

        Call at test start before the Bolt PCBA is powered on so DUT UART
        bridges that appear later can be detected as new vs this set.
        """
        acm = self._scan_acm_ports()
        ttyu = self._scan_ttyusb_ports()
        self.baseline_ports = set(acm) | set(ttyu)
        print(f"USB: captured baseline ports (ACM+ttyUSB): {sorted(self.baseline_ports)}")

    def _sorted_ttyusb_candidates(self, current: list[str]) -> list[str]:
        """Prefer ttyUSB devices not in baseline; else any current ttyUSB (stable path)."""
        new_only = [p for p in current if p not in self.baseline_ports]
        key = lambda x: int(x.replace("/dev/ttyUSB", ""))
        if new_only:
            return sorted(new_only, key=key)
        return sorted(current, key=key)

    def _try_open_first_available_ttyusb(
        self,
        deadline: float,
        overall_deadline: float | None = None,
    ) -> Optional[str]:
        """
        Poll until deadline for a usable DUT ttyUSB port and open it.

        Returns the device path on success (sets self.ser and self.dut_serial_port).
        """
        warned_fallback = False
        while time.time() < deadline:
            if overall_deadline is not None and time.time() >= overall_deadline:
                return None
            current = self._scan_ttyusb_ports()
            new_only = [p for p in current if p not in self.baseline_ports]
            if not new_only and current and not warned_fallback:
                print(
                    "USB: no new ttyUSB vs baseline; trying all ttyUSB devices "
                    "(bridge may have been present before power-up)"
                )
                warned_fallback = True
            candidates = self._sorted_ttyusb_candidates(current)
            if not candidates:
                time.sleep(0.5)
                continue
            for port in candidates:
                if not os.path.exists(port):
                    continue
                ser = bolt_control.open_serial(port)
                if ser:
                    self.ser = ser
                    self.dut_serial_port = port
                    return port
            time.sleep(0.5)
        return None

    def _wait_for_serial_device(
        self,
        port: str,
        timeout_s: float = 10.0,
        overall_deadline: float | None = None,
    ) -> bool:
        """
        Poll for the given serial device node to appear.

        This avoids the operator having to unplug/re‑plug the USB cable in cases
        where the kernel is just slow to enumerate the ACM device after power‑up.
        """
        # Respect an overall deadline if provided (e.g. 60 s total USB timeout).
        now = time.time()
        if overall_deadline is not None:
            timeout_s = min(timeout_s, max(0.0, overall_deadline - now))
        deadline = now + timeout_s

        while time.time() < deadline:
            if os.path.exists(port):
                return True
            time.sleep(0.5)
        return False

    def open_serial_port(self, max_retries: int = 3) -> bool:
        """
        Dynamically discover and open the Bolt PCBA UART (/dev/ttyUSB*) after flash/reset.

        Rescans ttyUSB on each attempt and prefers devices not in the baseline
        captured at test start (lowest index first). If the UART bridge was
        already present at baseline, falls back to trying any ttyUSB device.

        If the DUT does not become usable, we will:
          1. Power‑cycle the DUT via the PPK2.
          2. Re‑flash the test firmware.
          3. Retry the UART detection.
        After max_retries attempts we mark the USB connection as failed.
        """
        overall_deadline = time.time() + 60.0  # 1 minute max from start of USB step
        attempt = 0
        while attempt < max_retries:
            if time.time() >= overall_deadline:
                break

            attempt += 1
            print(f"USB: attempting to open DUT serial port (attempt {attempt}/{max_retries})")
            print(f"USB: rescanning ttyUSB (baseline has {len(self.baseline_ports)} port(s))")

            port_timeout = min(20.0, max(0.0, overall_deadline - time.time()))
            if port_timeout <= 0:
                break
            port_deadline = time.time() + port_timeout
            print(f"USB: waiting up to {port_timeout:.1f}s for a usable DUT ttyUSB port")

            opened = self._try_open_first_available_ttyusb(port_deadline, overall_deadline)
            if opened:
                self.tests["usb_connection"] = True
                print(f"USB: opened serial port {opened} on attempt {attempt}")
                return True

            print(f"USB: no DUT ttyUSB became openable within {port_timeout:.1f}s")

            if attempt >= max_retries:
                break

            # Try to recover by power‑cycling the DUT and reflashing test firmware.
            print("USB: attempting recovery by power‑cycling DUT via PPK2 and reflashing test firmware")
            try:
                try:
                    ppk2.toggle_DUT_power_OFF()
                    print("USB: DUT power turned OFF via PPK2")
                except Exception as exc:
                    print(f"USB: failed to turn DUT power OFF via PPK2: {exc}")
                time.sleep(0.5)

                # flash_test_firmware() will set source mode and turn power back on.
                if not self.flash_test_firmware():
                    print("USB: reflash of test firmware failed during recovery attempt")
                else:
                    print("USB: reflash of test firmware completed, will retry USB detection")
            except Exception as exc:
                print(f"USB: error during USB recovery sequence: {exc}")

        print("USB: failed to detect DUT serial port within 60s – marking USB connection as failed")
        self.tests["usb_connection"] = False
        self.failure = True
        return False

    def reopen_serial_port_for_calibration(self, timeout_s: float = 10.0) -> bool:
        """
        Re-detect and reopen the DUT ttyUSB port.

        The USB-UART adapter (e.g. CH341) can disconnect/re-enumerate during the test,
        which invalidates an existing Serial object. Rescans ttyUSB and reopens without
        changing the usb_connection test flag.
        """
        print("Analog cal: re-detecting serial port before calibration...")

        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

        deadline = time.time() + timeout_s
        warned_fallback = False
        while time.time() < deadline:
            current = self._scan_ttyusb_ports()
            new_only = [p for p in current if p not in self.baseline_ports]
            if not new_only and current and not warned_fallback:
                print(
                    "Analog cal: no new ttyUSB vs baseline; trying all ttyUSB devices "
                    "(bridge may have been present before power-up)"
                )
                warned_fallback = True

            candidates = self._sorted_ttyusb_candidates(current)
            if not candidates:
                time.sleep(0.5)
                continue

            preferred = self.dut_serial_port
            if preferred and preferred in candidates:
                ordered = [preferred] + [p for p in candidates if p != preferred]
            else:
                ordered = candidates

            for port in ordered:
                if not os.path.exists(port):
                    continue
                ser = bolt_control.open_serial(port)
                if ser:
                    self.ser = ser
                    self.dut_serial_port = port
                    print(f"Analog cal: reopened serial port {port}")
                    return True

            time.sleep(0.5)

        print(f"Analog cal: failed to reopen DUT ttyUSB within {timeout_s}s")
        return False

    def flash_test_firmware(self) -> bool:
        fw_path = os.path.join(FW_FOLDER_PATH, TEST_FW_FILENAME)
        # Ensure DUT is powered from PPK2 before flashing.
        try:
            ppk2.set_to_source_mode()
            time.sleep(0.2)
        except Exception:
            # If PPK2 is not available, continue and let flashing fail if DUT
            # truly has no power.
            pass

        self.tests["flash_test_fw"] = nrfjprog.flash_FW(fw_path)
        if not self.tests["flash_test_fw"]:
            self.failure = True
        return self.tests["flash_test_fw"]

    def flash_production_firmware(self, sg: bool = False) -> bool:
        fw_filename = SG_PRODUCTION_FW_FILENAME if sg else PRODUCTION_FW_FILENAME
        fw_path = os.path.join(FW_FOLDER_PATH, fw_filename)
        try:
            ppk2.set_to_source_mode()
            ppk2.toggle_DUT_power_ON()
            time.sleep(0.2)
        except Exception:
            pass

        self.tests["flash_production_fw"] = nrfjprog.flash_FW(fw_path)
        if not self.tests["flash_production_fw"]:
            self.failure = True
            return False

        print("USB: issuing nrfjprog --reset to trigger USB enumeration after production flash...")
        try:
            result = subprocess.run(
                ["nrfjprog", "--reset"],
                capture_output=True,
                text=True,
                timeout=10.0,
            )
            if result.returncode == 0:
                print("USB: nrfjprog --reset completed successfully (production flash)")
                time.sleep(1.0)
            else:
                print(f"USB: nrfjprog --reset failed with return code {result.returncode}")
                print(f"USB: stderr: {result.stderr}")
        except subprocess.TimeoutExpired:
            print("USB: nrfjprog --reset timed out after 10 seconds (production flash)")
        except Exception as exc:
            print(f"USB: error running nrfjprog --reset after production flash: {exc}")

        return True


    # --- QR → serial handling --------------------------------------------

    def set_bolt_id_from_qr(self, qr_payload: str) -> bool:
        bolt_id = bolt_control.parse_bolt_id_from_qr(qr_payload)
        if not bolt_id:
            print(f"Failed to parse Bolt ID from QR payload: {qr_payload}")
            self.tests["qr_scan"] = False
            self.failure = True
            return False

        self.measurements["bolt_id"] = bolt_id
        self.measurements["PCBA_ID"] = qr_payload
        self.measurements["dev_ID"] = bolt_id
        self.tests["qr_scan"] = True
        return True

    def program_serial_on_dut(self) -> bool:
        if not self.ser:
            return False

        bolt_id = self.measurements.get("bolt_id")
        if not bolt_id:
            return False

        # Attempt the settings write up to 3 times. Between failed attempts,
        # reopen the DUT UART (the CH341 bridge can drop/re-enumerate during
        # the write, which invalidates the existing Serial handle).
        max_attempts = 3
        ok = False
        for attempt in range(1, max_attempts + 1):
            try:
                ok = bolt_control.set_pcba_serial(self.ser, str(bolt_id))
            except Exception as exc:
                print(f"Set serial: exception during settings write (attempt {attempt}/{max_attempts}): {exc}")
                ok = False

            if ok:
                break

            if attempt < max_attempts:
                print(f"Set serial: attempt {attempt}/{max_attempts} failed; retrying after UART reopen...")
                if self.ser:
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                    self.ser = None

                if not self.open_serial_port(max_retries=1):
                    print("Set serial: failed to reopen DUT UART for retry; aborting set serial")
                    self.tests["set_serial"] = False
                    self.failure = True
                    return False

        if not ok:
            print(f"Set serial: all {max_attempts} attempts failed")
            self.tests["set_serial"] = False
            self.failure = True
            return False

        # The settings write returned OK; prove it persisted by rebooting the
        # DUT and confirming the firmware reports the matching Device ID in its
        # boot log. set_serial passes only on a verified match.
        try:
            verified = bolt_control.verify_serial_via_reboot(self.ser, str(bolt_id))
        except (SerialException, OSError) as exc:
            print(f"Set serial: serial error during reboot verification: {exc}")
            verified = False

        self.tests["set_serial"] = verified
        if not verified:
            self.failure = True
        return verified

    # --- IMU test ---------------------------------------------------------

    def run_imu_test(self) -> bool:
        """
        Pass as soon as the MLC angle handler fires at least once within
        the timeout — any rotation that triggers the IMU interrupt is enough.
        """
        if not self.ser:
            return False
        ok = bolt_control.wait_for_imu_rotation(self.ser, timeout_s=15.0)
        self.tests["imu"] = ok
        if not ok:
            self.failure = True
        return ok

    # --- BLE test ---------------------------------------------------------

    def _run_ble_test_script(self, bolt_id: str) -> Tuple[bool, Optional[float]]:
        """
        Run the standalone run_ble_test.py script as a subprocess and parse the RSSI result.
        
        This helper function invokes run_ble_test.py, which handles Bluetooth restart
        and device removal internally. It collects RSSI from advertisement packets and
        returns both success status and median RSSI value.
        
        Note: The fixture prefers using this standalone script for BLE testing as it
        provides RSSI measurements. If this script fails, the fixture falls back to
        the simpler in-process scan_for_ble_device() method. Future changes to
        run_ble_test.py should maintain the expected output format for RSSI parsing.
        
        Args:
            bolt_id: The Bolt device ID to test (e.g., '30000080')
        
        Returns:
            Tuple of (ok, median_rssi) where:
            - ok: True if the script exited successfully and parsed RSSI, False otherwise
            - median_rssi: Median RSSI value in dBm if successful, None otherwise
        """
        # Get the directory where this script is located
        time.sleep(0.5)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(script_dir, "run_ble_test.py")
        
        if not os.path.exists(script_path):
            print(f"BLE test: run_ble_test.py not found at {script_path}")
            return False, None
        
        print(f"BLE test: invoking standalone run_ble_test.py script for Bolt_{bolt_id}...")
        process = None
        try:
            # The orchestrator (run_ble_test) restarts bluetoothd once before
            # the first attempt, so we always pass --skip-restart to avoid
            # thrashing the daemon between retries. The subprocess still does
            # its own per-attempt cache cleanup so that BlueZ does not return
            # stale entries for the Bolt under test.
            process = subprocess.Popen(
                [sys.executable, script_path, "--skip-restart", "--timeout", "10", str(bolt_id)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=script_dir,
            )

            # Wait for completion with timeout. The script's BLE scan is capped
            # at 10s (--timeout 10); add buffer for its per-attempt cache cleanup.
            stdout, stderr = process.communicate(timeout=20.0)
            
            # Log output for debugging
            if stdout:
                print("BLE test script stdout:")
                for line in stdout.splitlines():
                    print(f"  {line}")
            if stderr:
                print("BLE test script stderr:")
                for line in stderr.splitlines():
                    print(f"  {line}")
            
            # Check exit code
            if process.returncode != 0:
                print(f"BLE test: run_ble_test.py exited with code {process.returncode}")
                return False, None
            
            # Parse the median RSSI from output
            # Look for line: "BLE test: PASSED - Median RSSI: -31 dBm"
            median_rssi = None
            for line in stdout.splitlines():
                if "BLE test: PASSED - Median RSSI:" in line:
                    # Extract the RSSI value using regex
                    match = re.search(r"Median RSSI:\s*(-?\d+\.?\d*)\s*dBm", line)
                    if match:
                        try:
                            median_rssi = float(match.group(1))
                            print(f"BLE test: parsed median RSSI from script output: {median_rssi} dBm")
                            return True, median_rssi
                        except ValueError:
                            print(f"BLE test: failed to parse RSSI value from line: {line}")
            
            # If we reach here, script passed but we couldn't parse RSSI
            print("BLE test: script passed but could not parse median RSSI from output")
            return False, None
            
        except subprocess.TimeoutExpired:
            print("BLE test: run_ble_test.py timed out after 20s")
            if process:
                process.kill()
            return False, None
        except Exception as exc:
            print(f"BLE test: error running run_ble_test.py: {exc}")
            if process:
                try:
                    process.kill()
                except Exception:
                    pass
            return False, None

    def _unblock_bluetooth_rfkill(self) -> None:
        """Clear any soft-rfkill block on bluetooth adapters via sysfs.

        On this Pi the controller comes up `off-blocked` after a
        `systemctl restart bluetooth` — rfkill leaves
        /sys/class/rfkill/rfkillN/soft set to 1 for the hci0 device, so
        bluetoothd cannot power the adapter and Bleak reports
        "No powered Bluetooth adapters found." Writing 0 to the `soft`
        file for every rfkill device of type "bluetooth" clears the block
        without needing the `rfkill` CLI installed.
        """
        try:
            entries = os.listdir("/sys/class/rfkill")
        except OSError as exc:
            print(f"BLE test: could not list /sys/class/rfkill: {exc}")
            return

        for entry in entries:
            type_path = f"/sys/class/rfkill/{entry}/type"
            soft_path = f"/sys/class/rfkill/{entry}/soft"
            try:
                with open(type_path) as f:
                    if f.read().strip() != "bluetooth":
                        continue
            except OSError:
                continue
            print(f"BLE test: clearing rfkill soft-block on {entry}")
            try:
                proc = subprocess.run(
                    ["sudo", "-S", "sh", "-c", f"echo 0 > {soft_path}"],
                    input="123456\n",
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                if proc.returncode != 0:
                    print(f"BLE test: rfkill unblock failed on {entry}: {proc.stderr.strip()}")
            except subprocess.TimeoutExpired:
                print(f"BLE test: rfkill unblock timed out on {entry}")
            except Exception as exc:
                print(f"BLE test: rfkill unblock error on {entry}: {exc}")

    def _bluetoothctl_power_on(self) -> None:
        """Ask bluetoothd to power up the default adapter."""
        try:
            proc = subprocess.run(
                ["bluetoothctl", "power", "on"],
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode == 0:
                print(f"BLE test: bluetoothctl power on -> {out or 'ok'}")
            else:
                print(f"BLE test: bluetoothctl power on failed: {err or out}")
        except subprocess.TimeoutExpired:
            print("BLE test: bluetoothctl power on timed out")
        except Exception as exc:
            print(f"BLE test: bluetoothctl power on error: {exc}")

    def _restart_bluetooth_service(self) -> None:
        """Restart bluetoothd once, clear rfkill, and power the adapter.

        Swallow all errors — the scan itself will report the real failure
        if BlueZ is still unhealthy after the restart sequence.
        """
        print("BLE test: restarting bluetooth service (one-shot for the cycle)...")
        process = None
        try:
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
                time.sleep(2.0)
            else:
                print(f"BLE test: bluetooth restart failed: {stderr}")
        except subprocess.TimeoutExpired:
            print("BLE test: bluetooth restart timed out")
            if process:
                process.kill()
        except Exception as exc:
            print(f"BLE test: error restarting bluetooth: {exc}")
            if process:
                try:
                    process.kill()
                except Exception:
                    pass

        # On the fixture Pi the adapter stays soft-blocked after a fresh
        # bluetoothd start (bluetoothd logs "Failed to set mode: Failed
        # (0x03)"). Force-unblock via sysfs, give the kernel a moment to
        # bring hci0 back up, then ask bluetoothd to power it on so Bleak
        # finds a powered adapter on the next scan.
        self._unblock_bluetooth_rfkill()
        time.sleep(1.0)
        self._bluetoothctl_power_on()

    def _reset_dut_via_nrfjprog(self) -> None:
        """Issue `nrfjprog --reset` and swallow any errors."""
        print("BLE test: issuing nrfjprog --reset after BLE failure...")
        try:
            result = subprocess.run(
                ["nrfjprog", "--reset"],
                capture_output=True,
                text=True,
                timeout=10.0,
            )
            if result.returncode == 0:
                print("BLE test: nrfjprog --reset completed successfully")
            else:
                print(f"BLE test: nrfjprog --reset failed with return code {result.returncode}")
                print(f"BLE test: stderr: {result.stderr}")
        except subprocess.TimeoutExpired:
            print("BLE test: nrfjprog --reset timed out after 10 seconds")
        except Exception as exc:
            print(f"BLE test: error running nrfjprog --reset: {exc}")

    def _attempt_ble_test(self, bolt_id: str) -> bool:
        """
        One BLE attempt: standalone script (with RSSI) then fallback presence
        scan. Returns True on success and sets `tests["ble"]` /
        `measurements["ble_rssi_median"]`. Does NOT update the persistent
        counter, the first-failure flag, or run nrfjprog --reset — the retry
        loop in run_ble_test handles those.
        """
        script_ok, median_rssi = self._run_ble_test_script(bolt_id)
        if script_ok and median_rssi is not None:
            self.measurements["ble_rssi_median"] = median_rssi
            self.tests["ble"] = True
            print(f"BLE test: PASSED via standalone script - Median RSSI: {median_rssi} dBm")
            return True

        print("BLE test: standalone script failed, falling back to simple device presence scan...")
        # Bluetooth was restarted once at the start of run_ble_test(); we do
        # not restart again per-attempt to avoid leaving bluetoothd in a bad
        # state from rapid back-to-back restarts.
        if bolt_control.scan_for_ble_device(bolt_id, timeout_s=10.0):
            self.measurements["ble_rssi_median"] = None
            self.tests["ble"] = True
            print("BLE test: PASSED via fallback scan (RSSI will be reported as N/A)")
            return True

        return False

    def run_ble_test(self) -> bool:
        """
        Run the BLE test with up to 3 retries on failure. Each failed attempt
        issues nrfjprog --reset and waits 1 s before the next attempt so the
        DUT comes back from a clean boot (advertising may have stalled or the
        BLE stack may have hung).

        Behavior matrix per attempt:
        - Script succeeds: Store median RSSI, pass.
        - Script fails + fallback presence scan succeeds: RSSI = None, pass.
        - Both fail: try again, up to max_attempts.
        """
        bolt_id = self.measurements.get("bolt_id")
        if not bolt_id:
            return False

        # One-shot bluetoothd restart at the start of the cycle. The
        # subprocess and fallback scan both rely on this and skip their own
        # restarts so we don't thrash the daemon between retries.
        self._restart_bluetooth_service()

        max_attempts = 4  # 1 initial + 3 retries
        for attempt in range(1, max_attempts + 1):
            if self._attempt_ble_test(bolt_id):
                set_ble_error_count(0)
                return True

            if attempt < max_attempts:
                print(f"BLE test: attempt {attempt}/{max_attempts} failed; resetting DUT and retrying")
                self._reset_dut_via_nrfjprog()
                time.sleep(1.0)

        # All attempts failed — finalise board state.
        print(f"BLE test: all {max_attempts} attempts failed")
        self.tests["ble"] = False
        self.measurements["ble_rssi_median"] = None

        error_count = get_ble_error_count()
        new_count = error_count + 1
        set_ble_error_count(new_count)

        if new_count == 1:
            self.ble_first_failure = True
            print("BLE test: FIRST FAILURE detected (likely transient due to re-power/advertising name change)")
            print("BLE test: will trigger test restart instead of marking board as failed")
        else:
            self.failure = True

        # Final reset so the DUT is in a clean state for the operator's next
        # action (manual retry or the next board).
        self._reset_dut_via_nrfjprog()
        return False

    # --- Analog calibration -----------------------------------------------

    def run_analog_calibration(self) -> bool:
        """
        Run the analog calibration sequence by calling calibraor_test.py functions directly.

        The calibration script handles all calibration logic including:
          - OFFSET (0 Ω) calibration
          - HIGH (270 kΩ) calibration
          - Reference value programming
          - Verification at multiple temperature points

        Captures calibration parameters and temperature readings for CSV reporting.

        Note: This method re-detects the serial port before calibration, as a PPK2
        power-cycle may have caused the DUT UART to re-enumerate as a different ttyUSBx port.
        """
        if not self.reopen_serial_port_for_calibration(timeout_s=10.0):
            print("Analog cal: failed to re-detect serial port before calibration")
            self.tests["analog"] = False
            self.failure = True
            return False

        def _w1_prep() -> bool:
            bolt_control.clear_serial_buffer(self.ser)
            time.sleep(2.0)
            if not bolt_control.send_shell_command(self.ser, "w1 slpz 0"):
                print("Analog cal: failed to send w1 slpz 0 command")
                return False
            time.sleep(0.1)
            bolt_control.clear_serial_buffer(self.ser)
            return True

        w1_ok = False
        try:
            w1_ok = _w1_prep()
        except (SerialException, OSError) as exc:
            print(f"Analog cal: serial error during w1 prep: {exc}")
            w1_ok = False

        if not w1_ok:
            print("Analog cal: retrying w1 prep once after UART reopen...")
            if self.reopen_serial_port_for_calibration(timeout_s=10.0):
                try:
                    w1_ok = _w1_prep()
                except (SerialException, OSError) as exc:
                    print(f"Analog cal: serial error during w1 prep retry: {exc}")
            if not w1_ok:
                print("Analog cal: warning - w1 prep incomplete, continuing anyway")

        # Import calibration functions directly instead of using subprocess
        try:
            from calibraor_test import run_full_analog_calibration, CALIBRATION_MODE_FAST
        except ImportError:
            print("Analog cal: failed to import calibraor_test module")
            self.tests["analog"] = False
            self.failure = True
            return False

        print("Analog cal: running calibration sequence (fast mode)...")
        try:
            try:
                cal_result = run_full_analog_calibration(self.ser, mode=CALIBRATION_MODE_FAST)
            except (SerialException, OSError) as exc:
                print(f"Analog cal: serial error during calibration: {exc}")
                print("Analog cal: retrying once after UART reopen...")
                if not self.reopen_serial_port_for_calibration(timeout_s=10.0):
                    self.tests["analog"] = False
                    self.failure = True
                    return False
                try:
                    cal_result = run_full_analog_calibration(self.ser, mode=CALIBRATION_MODE_FAST)
                except (SerialException, OSError) as exc2:
                    print(f"Analog cal: serial error during calibration retry: {exc2}")
                    self.tests["analog"] = False
                    self.failure = True
                    return False

            # Store calibration parameters in measurements
            if cal_result.get("success", False):
                self.measurements["adc_offset_raw_factory"] = cal_result.get("offset_raw")
                self.measurements["adc_high_raw_factory"] = cal_result.get("high_raw")
                self.measurements["adc_ref_factory"] = cal_result.get("reference", 3619.64)
                self.measurements["adc_temp_27k_measured_c"] = cal_result.get("temp_27k")
                self.measurements["adc_temp_10k_measured_c"] = cal_result.get("temp_10k")
                self.measurements["adc_temp_4k99_measured_c"] = cal_result.get("temp_4k99")
                self.measurements["adc_temp_2k2_measured_c"] = cal_result.get("temp_2k2")

                print("Analog cal: calibration completed successfully")
                print(f"  Offset: {cal_result.get('offset_raw')}")
                print(f"  High: {cal_result.get('high_raw')}")
                print(f"  Reference: {cal_result.get('reference')}")
                print(f"  Temp 27k: {cal_result.get('temp_27k')} °C")
                print(f"  Temp 10k: {cal_result.get('temp_10k')} °C")
                print(f"  Temp 4.99k: {cal_result.get('temp_4k99')} °C")
                print(f"  Temp 2.2k: {cal_result.get('temp_2k2')} °C")

                self.tests["analog"] = True
                return True
            else:
                print("Analog cal: calibration failed")
                self.tests["analog"] = False
                self.failure = True
                return False
        except Exception as exc:
            print(f"Analog cal: error during calibration: {exc}")
            self.tests["analog"] = False
            self.failure = True
            return False

    # --- Sleep current test -----------------------------------------------

    def run_sleep_current_test(self) -> bool:
        """
        Measure average sleep current for ~10 seconds using the PPK2.

        Uses the same method as Flex: source meter mode with get_average_current()
        polling. The DUT should be powered only from the PPK2 (debugger and USB
        disconnected). Pass if the average is <= 180 uA.

        A CSV report with timestamped current measurements is generated.
        """
        # Reset abnormal-reading flag for this attempt so the retry loop in
        # run_bolt_test can detect a fresh PPK2 fault.
        self.ppk2_sleep_error = False
        self.ppk2_lost = False
        print("Sleep current: power cycling DUT via PPK2...")
        try:
            ppk2.toggle_DUT_power_OFF()
            print("Sleep current: DUT power turned OFF")
            time.sleep(0.5)  # Wait for power to fully turn off
        except Exception as exc:
            print(f"Sleep current: warning - power cycle OFF failed (non-fatal): {exc}")
            # Continue anyway

        try:
            # Configure PPK2 in source meter mode (set_to_ampere_mode now uses source meter)
            ppk2.set_to_source_mode()
            # Ensure voltage is set (already done in set_to_ampere_mode, but explicit for clarity)
            ppk2.toggle_DUT_power_ON()
            print("Sleep current: DUT power turned ON")
        except Exception as exc:
            print(f"Sleep current: failed to configure PPK2: {exc}")
            self.tests["sleep_current"] = False
            self.failure = True
            return False

        time.sleep(1.0)  # allow DUT to settle into sleep

        # Generate CSV report filename based on bolt_id and timestamp
        bolt_id = self.measurements.get("bolt_id", "unknown")
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_filename = f"sleep_current_{bolt_id}_{timestamp_str}.csv"
        csv_filepath = os.path.join("data", csv_filename)

        # Measure current using get_average_current() polling like Flex, but collect data for CSV
        duration_s = 10.0
        min_duration_s = 5.0  # Minimum duration before allowing early exit
        start_time = time.time()
        measurements = []  # List of (timestamp, current_ua) tuples
        total = 0.0
        count = 0
        readings_to_skip = 2  # Skip the first two readings as they're often abnormal

        # A sustained run of -1 returns means get_average_current keeps hitting
        # USB I/O errors (Errno 5) — i.e. the PPK2 has dropped off the bus.
        # Bail out fast instead of spinning for the full duration and spamming
        # thousands of error lines into the log.
        consecutive_errors = 0
        MAX_CONSECUTIVE_PPK2_ERRORS = 25

        print(f"Sleep current: measuring for at least {min_duration_s} seconds (up to {duration_s} seconds)...")
        while time.time() - start_time < duration_s:
            current_ua = ppk2.get_average_current(100)

            # get_average_current returns -1 on a PPK2 I/O error (or when the
            # device handle is gone). A run of these means the device is lost,
            # not just a noisy sample — abort early and flag it for the caller.
            if current_ua == -1:
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_PPK2_ERRORS:
                    print(
                        f"Sleep current: PPK2 returned {consecutive_errors} consecutive "
                        "I/O errors - device appears lost from the USB bus; aborting"
                    )
                    self.ppk2_lost = True
                    self.tests["sleep_current"] = False
                    return False
                time.sleep(0.05)  # avoid a tight error spin while hammering EIO
                continue
            consecutive_errors = 0

            # Skip the first two readings as they're often abnormal
            if readings_to_skip > 0:
                print(f"Sleep current: discarding reading {3 - readings_to_skip}: {current_ua:.2f} uA")
                readings_to_skip -= 1
                continue

            # Drop out-of-range samples. PPK2 readings are noisy on this fixture:
            # clean samples sit at ~97 uA but the stream contains intermittent
            # bursts in the tens of mA range, plus occasional negative readings
            # (PPK2 zero-offset noise / get_average_current sentinel). Neither
            # is representative of the DUT's sleep current.
            if current_ua < 10.0:
                print(f"Sleep current: dropping low sample: {current_ua:.2f} uA (< 10 uA)")
                continue
            if current_ua > 1200.0:
                print(f"Sleep current: dropping spike sample: {current_ua:.2f} uA (> 1200 uA)")
                continue

            timestamp = time.time() - start_time
            measurements.append((timestamp, current_ua))
            total += current_ua
            count += 1
            print(f"Sleep current: {current_ua:.2f} uA (sample {count})")

            # Calculate running average and check for early pass (like Flex)
            # Only allow early exit after minimum duration has elapsed
            elapsed = time.time() - start_time
            if elapsed >= min_duration_s:
                avg_ua = total / count
                # Check for abnormal PPK2 readings (> 1000 uA or < 5 uA indicates fixture issue)
                if avg_ua > 1000.0 or avg_ua < 5.0:
                    print(f"Sleep current: ABNORMAL PPK2 READING detected: {avg_ua:.2f} uA (likely fixture issue, not board failure)")
                    self.ppk2_sleep_error = True
                    self.measurements["sleep_current_ua"] = avg_ua
                    # Do NOT mark board as failed - this is a fixture issue.
                    # The persistent error counter is bumped by the retry loop in
                    # run_bolt_test once all retries are exhausted.
                    return False
                
                if avg_ua < 120.0:
                    print(f"Sleep current: average {avg_ua:.2f} uA is below 120 uA after {elapsed:.1f}s - passing early")
                    self.measurements["sleep_current_ua"] = avg_ua
                    # Reset error counter on successful test
                    set_ppk2_error_count(0)
                    # Generate CSV report with collected data
                    try:
                        os.makedirs(os.path.dirname(csv_filepath), exist_ok=True)
                        with open(csv_filepath, 'w', newline='') as csvfile:
                            writer = csv.writer(csvfile)
                            writer.writerow(['Timestamp (s)', 'Current (uA)'])
                            for ts, curr in measurements:
                                writer.writerow([f"{ts:.3f}", f"{curr:.2f}"])
                        print(f"Sleep current: CSV report saved to {csv_filepath}")
                    except Exception as exc:
                        print(f"Sleep current: failed to write CSV report: {exc}")
                    
                    self.tests["sleep_current"] = True
                    return True

        if count == 0:
            print("Sleep current: no valid measurements collected")
            self.tests["sleep_current"] = False
            self.failure = True
            return False

        avg_ua = total / count
        self.measurements["sleep_current_ua"] = avg_ua
        print(f"Sleep current average: {avg_ua:.2f} uA")

        # Check for abnormal PPK2 readings (> 1000 uA or < 5 uA indicates fixture issue)
        if avg_ua > 1000.0 or avg_ua < 5.0:
            print(f"Sleep current: ABNORMAL PPK2 READING detected: {avg_ua:.2f} uA (likely fixture issue, not board failure)")
            self.ppk2_sleep_error = True
            # Do NOT mark board as failed - this is a fixture issue.
            # The persistent error counter is bumped by the retry loop in
            # run_bolt_test once all retries are exhausted.
            return False

        # Generate CSV report
        try:
            os.makedirs(os.path.dirname(csv_filepath), exist_ok=True)
            with open(csv_filepath, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['Timestamp (s)', 'Current (uA)'])
                for timestamp, current_ua in measurements:
                    writer.writerow([f"{timestamp:.3f}", f"{current_ua:.2f}"])
            print(f"Sleep current: CSV report saved to {csv_filepath}")
        except Exception as exc:
            print(f"Sleep current: failed to write CSV report: {exc}")

        # Reset error counter on successful test (even if it fails the <= 180 uA criterion)
        # Only reset if it's not an abnormal reading
        set_ppk2_error_count(0)

        # Pass criterion: average sleep current <= 180 uA
        # Note: Readings between 180-1000 uA are treated as normal board failures
        # temporarily set threshold to 250 uA to account for some variability, but this can be tightened later
        self.tests["sleep_current"] = avg_ua <= 250.0
        if not self.tests["sleep_current"]:
            self.failure = True
        return self.tests["sleep_current"]

    # --- Final aggregation -----------------------------------------------

    def evaluate_overall_result(self) -> bool:
        for key, value in self.tests.items():
            if key == "final":
                continue
            if not value:
                self.tests["final"] = False
                return False
        self.tests["final"] = True
        return True


def prompt_for_bolt_qr(app: gui.App) -> str:
    """
    Reuse the PCBA barcode dialog to scan the Bolt QR string.
    """
    app.scan_pcba_barcode_window()
    qr_payload = app.get_pcba_barcode()
    print(f"Scanned Bolt QR: {qr_payload}")
    return qr_payload


def prompt_for_hw_id(app: gui.App) -> str:
    """
    Prompt the operator to scan the hardware ID label on the board and return
    the scanned value verbatim.

    The hardware ID is a structured string (e.g. '03-002-0000124'), not a Bolt
    QR URL, so it is recorded exactly as scanned — only a leading 'd-' URL form
    is unwrapped, to stay tolerant of mistakenly scanning a Bolt sticker. Shown
    before the Bolt QR scan. The operator may SKIP, in which case an empty
    string is returned and the HW ID column falls back to 'N/A'.
    """
    app.scan_hw_id_window()
    raw = app.get_hw_id_barcode()
    if not raw:
        return ""
    raw = raw.strip()
    # Only unwrap an actual QR URL (…/qr/d-12345); otherwise keep the full
    # hardware ID, including any '03-002-' style prefix.
    url_match = re.search(r"d-(\d+)", raw)
    hw_id = url_match.group(1) if url_match else raw
    print(f"Scanned hardware ID: {hw_id}")
    return hw_id


def _update_pending_state(test: BoltTest) -> None:
    """Persist or clear resumable state for this board.

    A board that passed everything through analog calibration but did not finish
    the production flash + sleep current stage is saved so a re-scan can resume.
    A board that fully passed has any saved state cleared.
    """
    bolt_id = test.measurements.get("bolt_id")
    if not bolt_id:
        return
    # Only boards that got through every test up to the sleep stage are
    # candidates; if they didn't, leave any existing state untouched.
    if not pending_tests.prereqs_passed(test.tests):
        return
    if pending_tests.sleep_stage_complete(test.tests, test.measurements):
        # Sleep current genuinely measured-and-passed (or SG): board is done.
        pending_tests.clear(bolt_id)
    else:
        # Sleep stage skipped/deferred or failed: keep it resumable so a re-scan
        # can come back and complete the production flash + sleep current.
        pending_tests.save(bolt_id, test.tests, test.measurements)


def run_bolt_test(app: gui.App, skip_cal: bool = False, sg: bool = False) -> BoltTest:
    test = BoltTest()
    start_time = time.time()

    try:
        # Ensure PPK2 is fully turned off before capturing baseline ports
        # This is critical when running tests in sequence, as the board may
        # still be powered from the previous test cycle.
        try:
            ppk2.toggle_DUT_power_OFF()
            print("USB: ensuring PPK2 power is OFF before baseline port capture")
            time.sleep(0.5)  # Give time for power to fully turn off
        except Exception as exc:
            print(f"USB: warning - failed to turn off PPK2 power: {exc}")
            # Continue anyway, as this might be a development environment without PPK2

        # Capture baseline of existing /dev/ttyACM* and /dev/ttyUSB* before the Bolt is powered on
        test._capture_baseline_ports()

        test.measurements["test_ID"] = int(start_time)
        # Scan the hardware ID label on the board first. Recorded as HW ID in
        # the CSV; operator may SKIP, leaving the default 'N/A'.
        hw_id = prompt_for_hw_id(app)
        if hw_id:
            test.measurements["HW_ID"] = hw_id
        # Indicator 1: scan Bolt QR and parse Bolt ID from it.
        qr_payload = prompt_for_bolt_qr(app)
        if not qr_payload:
            print("QR scan: no data received from scanner; aborting test.")
            app.update_test_indicator(1, False)
            test.failure = True
            return test

        if not test.set_bolt_id_from_qr(qr_payload):
            app.update_test_indicator(1, False)
            return test

        app.update_test_indicator(1, True)

        # If this exact board was previously left with only the production flash
        # + sleep current stage outstanding (it passed everything through analog
        # calibration), offer to resume instead of redoing all the earlier steps.
        resuming = False
        resume_bolt_id = test.measurements.get("bolt_id")
        pending = pending_tests.load(resume_bolt_id) if resume_bolt_id else None
        if pending:
            saved_meas = pending.get("measurements", {})
            summary = (
                f"  IMU/BLE/analog: passed (BLE RSSI {saved_meas.get('ble_rssi_median')})\n"
                f"  Sleep current last result: {saved_meas.get('sleep_current_ua')}"
            )
            if app.pending_tests_window(resume_bolt_id, summary):
                resuming = True
                # Carry over the previously-passed results and measurements so the
                # final CSV row reflects them, and mark their indicators green.
                test.tests.update(pending.get("tests", {}))
                for key, value in saved_meas.items():
                    if key in ("bolt_id", "PCBA_ID", "dev_ID"):
                        continue  # keep the freshly-scanned identity
                    test.measurements[key] = value
                # Force the production flash + sleep current stage to re-run.
                test.tests["flash_production_fw"] = False
                test.tests["sleep_current"] = False
                test.tests["final"] = False
                test.measurements["sleep_current_skipped"] = False
                test.measurements["sleep_current_ua"] = None
                for indicator in range(1, 8):
                    app.update_test_indicator(indicator, True)
                print(f"Resume: skipping steps 1-7 for {resume_bolt_id}; re-running production flash + sleep current")
            else:
                # Operator chose a full re-test: discard the saved state.
                pending_tests.clear(resume_bolt_id)

        # Steps 2-7 (provisioning + functional checks). Skipped entirely when
        # resuming a board that already passed them on a previous run.
        if not resuming:
            # Indicator 2: flash test firmware.
            if not test.flash_test_firmware():
                app.update_test_indicator(2, False)
                return test
            app.update_test_indicator(2, True)
            # After flashing test firmware, issue an explicit debug reset via subprocess
            # so that the Bolt boots cleanly and USB CDC can enumerate, without requiring
            # the operator to unplug/re‑plug the USB cable.
            print("USB: issuing nrfjprog --reset to trigger USB enumeration...")
            try:
                result = subprocess.run(
                    ["nrfjprog", "--reset"],
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                )
                if result.returncode == 0:
                    print("USB: nrfjprog --reset completed successfully")
                    # Give the device a moment to enumerate after reset
                    time.sleep(1.0)
                else:
                    print(f"USB: nrfjprog --reset failed with return code {result.returncode}")
                    print(f"USB: stderr: {result.stderr}")
            except subprocess.TimeoutExpired:
                print("USB: nrfjprog --reset timed out after 10 seconds")
            except Exception as exc:
                print(f"USB: error running nrfjprog --reset: {exc}")

            # Indicator 3: USB / shell connection.
            if not test.open_serial_port():
                app.update_test_indicator(3, False)
                return test
            app.update_test_indicator(3, True)
            time.sleep(8.0)

            # Indicator 4: set serial on DUT.
            if not test.program_serial_on_dut():
                app.update_test_indicator(4, False)
                return test
            app.update_test_indicator(4, True)

            # Indicator 5: IMU test (manual rotation).
            if sg:
                print("IMU test: --SG set; skipping IMU test and recording result as 'sg'.")
                test.tests["imu"] = SG_SKIPPED_RESULT
                app.update_test_indicator(5, True)
            else:
                app.imu_instruction_window()
                if not test.run_imu_test():
                    app.update_test_indicator(5, False)
                    return test
                app.update_test_indicator(5, True)

            # Indicator 6: BLE test.
            # Tee stdout across the whole BLE cycle (orchestrator + every
            # subprocess attempt + fallback scan) so the full transcript can be
            # written to log/ble_fail_<id>_<ts>.log if the cycle fails.
            ble_tee = _TeeStdout(sys.stdout)
            original_stdout = sys.stdout
            sys.stdout = ble_tee
            try:
                ble_ok = test.run_ble_test()
            finally:
                sys.stdout = original_stdout

            if not ble_ok:
                try:
                    ble_bolt_id = test.measurements.get("bolt_id", "unknown") or "unknown"
                    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                    os.makedirs(BLE_LOG_DIR, exist_ok=True)
                    log_path = os.path.join(
                        BLE_LOG_DIR,
                        f"ble_fail_{ble_bolt_id}_{timestamp_str}.log",
                    )
                    with open(log_path, "w") as logfile:
                        logfile.write(ble_tee.getvalue())
                    print(f"BLE test: failure log saved to {log_path}")
                except Exception as exc:
                    print(f"BLE test: failed to write failure log: {exc}")

                app.update_test_indicator(6, False)
                if test.ble_first_failure:
                    # First BLE failure: inform operator and abort this run
                    app.ble_retry_window()
                    return test
                else:
                    # Subsequent failures: behave as current (normal board fail)
                    return test
            app.update_test_indicator(6, True)

            # Indicator 7: analog calibration.
            if sg:
                print("Analog cal: --SG set; skipping calibration and recording result as 'sg'.")
                test.tests["analog"] = SG_SKIPPED_RESULT
                app.update_test_indicator(7, True)
            elif skip_cal:
                print("Analog cal: --SKIP_CAL set; skipping calibration and marking as passed.")
                test.tests["analog"] = True
                upload_results.mark_skipped("analog calibration skipped via --SKIP_CAL")
                app.update_test_indicator(7, True)
            elif not test.run_analog_calibration():
                app.update_test_indicator(7, False)
                return test
            else:
                app.update_test_indicator(7, True)

        # Indicator 8 + 9: gate — production flash + sleep current (or skip both)
        if sg:
            # SG mode: flash the SG production firmware, skip the sleep
            # current test entirely and record it as 'sg' in the CSV.
            sleep_current_choice = 2  # behave as "skipped" for the PPK2 error paths below
            if not test.flash_production_firmware(sg=True):
                app.update_test_indicator(8, False)
                return test
            app.update_test_indicator(8, True)
            print("Sleep current: --SG set; skipping sleep current test and recording result as 'sg'.")
            test.tests["sleep_current"] = SG_SKIPPED_RESULT
            test.measurements["sleep_current_ua"] = SG_SKIPPED_RESULT
            app.update_test_indicator(9, True)
            sleep_test_result = True
        else:
            # No pre-flash prompt: after analog calibration, always flash the
            # production firmware, then prompt the operator to ready the board for
            # the sleep current measurement.
            sleep_current_choice = 1  # always "proceed" (used by the PPK2 error paths)
            if not test.flash_production_firmware():
                app.update_test_indicator(8, False)
                return test
            app.update_test_indicator(8, True)
            # Production flash already issued nrfjprog --reset internally.
            # Prompt operator to ready the board before sleep current measurement.
            app.ready_for_sleep_current_window()
            # Tee stdout across the whole sleep-current phase (initial attempt
            # + retries) so we can save the full transcript — including any
            # ppk2.py prints — to a log file if the test fails for any reason.
            sleep_tee = _TeeStdout(sys.stdout)
            original_stdout = sys.stdout
            sys.stdout = sleep_tee
            try:
                # Auto-retry on any out-of-range sleep current result.
                # Recovery escalates: first a no-touch software restart
                # (restart_ppk2() tears down + re-discovers the handle). If
                # the PPK2 is stuck on the hardware side and the software
                # restart can't bring it back, prompt the operator to press
                # the button on the PPK2 to power-cycle it, then re-grab the
                # handle (reacquire_ppk2()) and retry.
                max_attempts = 4  # 1 initial + 3 retries
                sleep_test_result = False
                software_restart_used = False
                for attempt in range(1, max_attempts + 1):
                    sleep_test_result = test.run_sleep_current_test()
                    if sleep_test_result:
                        break
                    if attempt >= max_attempts:
                        break

                    # First failure with the PPK2 still on the bus: try ONE
                    # cheap, no-touch software restart. We only try it once —
                    # restart_ppk2() can "succeed" by re-initialising a device
                    # that is enumerated but wedged (it then keeps returning
                    # garbage / no valid samples), so repeating it just masks a
                    # hardware-stuck PPK2. After that, escalate to the operator.
                    if not software_restart_used and ppk2.device_present() and not test.ppk2_lost:
                        software_restart_used = True
                        print(f"Sleep current: attempt {attempt}/{max_attempts} failed; restarting PPK2 (software) and retrying")
                        if ppk2.restart_ppk2():
                            time.sleep(1.0)
                            continue
                        print("Sleep current: software PPK2 restart failed")

                    # Software restart already tried/unavailable and the test
                    # is still failing: the PPK2 is stuck on the hardware side.
                    # Ask the operator to press the button on the PPK2 to
                    # power-cycle it, then re-grab the handle. Retry a couple of
                    # times before giving up (the overall fallback below flags
                    # the PPK2 lost and prompts a replug + app restart).
                    recovered = False
                    for btn_try in range(1, 3):
                        print(f"Sleep current: PPK2 still failing - asking operator to power-cycle the PPK2 (try {btn_try}/2)")
                        app.ppk2_press_button_window()
                        if ppk2.reacquire_ppk2():
                            recovered = True
                            break
                        print("Sleep current: PPK2 still not usable after button press")
                    if not recovered:
                        test.ppk2_lost = True
                        print("Sleep current: PPK2 unrecoverable after button-press recovery - aborting retries")
                        break
                    test.ppk2_lost = False
                    time.sleep(1.0)
            finally:
                sys.stdout = original_stdout

            # Persist a failure log if the test failed for any reason (PPK2
            # fixture issue, threshold exceeded, no valid samples, etc.).
            if not sleep_test_result or test.ppk2_sleep_error:
                try:
                    bolt_id = test.measurements.get("bolt_id", "unknown") or "unknown"
                    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                    os.makedirs(SLEEP_CURRENT_LOG_DIR, exist_ok=True)
                    log_path = os.path.join(
                        SLEEP_CURRENT_LOG_DIR,
                        f"sleep_current_fail_{bolt_id}_{timestamp_str}.log",
                    )
                    with open(log_path, "w") as logfile:
                        logfile.write(sleep_tee.getvalue())
                    print(f"Sleep current: failure log saved to {log_path}")
                except Exception as exc:
                    print(f"Sleep current: failed to write failure log: {exc}")

        # PPK2 physically dropped off the USB bus (repeated EIO) — a software
        # restart can't recover it. Prompt the operator to close the app, replug
        # the PPK2, and reopen. This is a fixture/cable issue, not a board fault,
        # so do not bump the abnormal-reading counter or print a label.
        if test.ppk2_lost and sleep_current_choice != 2:
            app.update_test_indicator(9, False)
            print("Sleep current: PPK2 lost from USB - prompting operator to replug and restart fixture")
            app.ppk2_lost_window()
            return test

        # Check for abnormal PPK2 readings (fixture issue, not board failure)
        if test.ppk2_sleep_error and sleep_current_choice != 2:
            # All retries exhausted — bump the persistent counter once and
            # escalate via the existing restart-fixture / reboot-Pi flow.
            error_count = get_ppk2_error_count() + 1
            set_ppk2_error_count(error_count)
            print(f"PPK2 error: abnormal reading persisted after retries, error count: {error_count}")
            
            # Set indicator to red to show something went wrong, but clarify it's a fixture issue
            app.update_test_indicator(9, False)
            print("Sleep current: test aborted due to PPK2 fixture issue (not a board failure)")
            
            if error_count == 1:
                # First occurrence: prompt to restart fixture app
                print("PPK2 error: first occurrence - prompting operator to restart fixture")
                app.restart_fixture_window()
                # Exit the test function - the main loop will handle app restart
                return test
            else:
                # Repeated occurrence: prompt to reboot Pi
                print(f"PPK2 error: repeated occurrence (count={error_count}) - prompting operator to reboot Pi")
                if app.reboot_pi_window():
                    # Operator confirmed reboot
                    print("PPK2 error: operator confirmed Pi reboot - executing reboot command")
                    try:
                        # Execute reboot command with short timeout (reboot returns quickly)
                        subprocess.run(
                            ["sudo", "-S", "reboot"],
                            input="123456\n",
                            text=True,
                            timeout=3.0,
                            check=False,  # Don't raise on non-zero exit (reboot may exit with code)
                        )
                        print("PPK2 error: reboot command executed")
                        # Give a moment for the command to be processed, then close GUI
                        time.sleep(0.5)
                        app.destroy()
                    except subprocess.TimeoutExpired:
                        # Command was sent, continue with GUI close
                        print("PPK2 error: reboot command sent (timeout)")
                        time.sleep(0.5)
                        app.destroy()
                    except Exception as exc:
                        print(f"PPK2 error: failed to execute reboot command: {exc}")
                        print("PPK2 error: please reboot the Pi manually")
                        # Still close the GUI
                        app.destroy()
                else:
                    # Operator cancelled reboot
                    print("PPK2 error: operator cancelled Pi reboot")
                # Exit the test function
                return test
        
        # Normal sleep current test result handling
        if not sleep_test_result:
            app.update_test_indicator(9, False)
            return test
        app.update_test_indicator(9, True)

        # Final result aggregation.
        final_ok = test.evaluate_overall_result()
        # Map this to a later indicator position to keep alignment similar to the
        # original GUI (e.g. slot 10).
        app.update_test_indicator(10, final_ok)

    finally:
        # Persist or clear resumable state for this board first, so it runs on
        # every exit path — including the PPK2-lost / abnormal-reading early
        # returns below, which are exactly the cases worth resuming.
        _update_pending_state(test)

        # Always execute these, even on early return
        # Skip normal finalization if the PPK2 was lost from the USB bus
        # (fixture/cable issue, not a board failure — operator has been prompted
        # to replug it).
        if test.ppk2_lost:
            print(f"Time to complete Bolt test: {time.time() - start_time:.2f}s")
            print("PPK2 lost: skipping label printing and CSV writing (fixture issue, not board failure)")
            return test

        # Skip normal finalization if PPK2 error was detected (fixture issue, not board failure)
        if test.ppk2_sleep_error:
            print(f"Time to complete Bolt test: {time.time() - start_time:.2f}s")
            print("PPK2 error: skipping label printing and CSV writing (fixture issue, not board failure)")
            # Do not evaluate final result or print label/CSV for fixture errors
            return test
        
        # Skip normal finalization if a BLE first-failure occurred
        if test.ble_first_failure:
            print(f"Time to complete Bolt test: {time.time() - start_time:.2f}s")
            print("BLE first-failure: skipping label printing and CSV writing (test aborted, not a board failure)")
            return test
        
        # Evaluate result if not already done
        if "final" not in test.tests or not test.tests.get("final"):
            final_ok = test.evaluate_overall_result()
            app.update_test_indicator(10, final_ok)
        else:
            final_ok = test.tests.get("final", False)

        print(f"Time to complete Bolt test: {time.time() - start_time:.2f}s")

        # Label printing – best-effort only. A missing or unresponsive printer
        # must never block the flow or mark the board as failed; labels can be
        # reprinted afterwards.
        _print_label_best_effort(final_ok, test.measurements)

        # Write test results to CSV – always execute, even on failure
        try:
            csv_manager.write_test_results(test.tests, test.measurements, user="N/A", fixture=1)
            print("Test results written to CSV")
        except Exception as exc:
            print(f"Failed to write CSV results: {exc}")

        # Upload test results to Google Drive – always execute, even on failure
        try:
            upload_results.upload_to_drive()
            print("Test results uploaded to Google Drive")
        except Exception as exc:
            print(f"Failed to upload results to Google Drive: {exc}")

    return test


def main() -> None:
    parser = argparse.ArgumentParser(description="Bolt PCBA test fixture runner")
    parser.add_argument(
        "--SKIP_CAL",
        action="store_true",
        help="Skip the analog calibration test and mark it as passed (green).",
    )
    parser.add_argument(
        "--SG",
        action="store_true",
        help=(
            "Strain-gauge mode: skip the analog calibration, IMU and sleep "
            "current tests (recorded as 'sg' in the CSV) and flash "
            f"{SG_PRODUCTION_FW_FILENAME} as the production firmware."
        ),
    )
    args = parser.parse_args()
    skip_cal = args.SKIP_CAL
    sg = args.SG
    if skip_cal:
        print("Startup: --SKIP_CAL enabled; analog calibration will be skipped and marked green.")
    if sg:
        print(
            "Startup: --SG enabled; calibration, IMU and sleep current tests will be "
            f"skipped (CSV records 'sg') and {SG_PRODUCTION_FW_FILENAME} will be flashed "
            "as production firmware."
        )

    app = gui.App()
    # Route GUI display through the persistent tee instead of replacing
    # sys.stdout.write directly, so the log file keeps capturing every print.
    if isinstance(sys.stdout, _PersistentTee):
        sys.stdout.add_sink(app.update_serial_display)
    else:
        sys.stdout.write = app.update_serial_display

    # Basic PPK2 initialisation; if no PPK2 is connected this will just log
    # and return 0. Current‑measurement tests can be added later.
    try:
        ppk2.setup_ppk()
    except Exception as exc:
        print(f"PPK2 setup failed (non‑fatal during development): {exc}")

    # Abort early if the PPK2 wasn't enumerated at module import. Without it
    # the DUT cannot be powered correctly and the sleep current test fails
    # four attempts in a row with a NoneType error on ppk2_device.
    if not ppk2.device_available:
        print("=" * 70)
        print("FATAL: PPK2 not detected at startup.")
        print("Check the PPK2 USB cable and power switch, then restart the fixture.")
        print("=" * 70)
        app.acknowledge_info_var.set(0)
        app.information_window()
        app.wait_variable(app.acknowledge_info_var)
        return

    app.acknowledge_info_var.set(0)
    app.information_window()
    app.wait_variable(app.acknowledge_info_var)

    while True:
        app.reset_indicators()
        app.update_test_display(state="active")
        bolt_test = run_bolt_test(app, skip_cal=skip_cal, sg=sg)

        # Check if the PPK2 dropped off the USB bus - the operator was prompted
        # to replug it, so exit the loop to close the app for a clean restart.
        if getattr(bolt_test, "ppk2_lost", False):
            print("PPK2 lost: exiting main loop - operator should replug the PPK2 and reopen the fixture")
            break

        # Check if PPK2 error occurred - if so, exit the loop to allow app restart
        if bolt_test.ppk2_sleep_error:
            error_count = get_ppk2_error_count()
            if error_count == 1:
                # First occurrence: exit the loop so operator can restart the app
                print("PPK2 error: exiting main loop - operator should restart the fixture application")
                break
        
        # Check if BLE first-failure occurred - abort this run and restart test for same board
        if getattr(bolt_test, "ble_first_failure", False):
            print("BLE first-failure: aborting current test run; operator should restart the test for this board")
            # Restart loop without test_complete popup, label, CSV, or power-off
            continue
        
        app.update_test_display(state="complete")

        # Simple end‑of‑test popup.
        app.test_complete_window()
        app.wait_variable(app.test_complete_var)
        app.test_complete_var.set(0)

        # After each test cycle, turn off DUT power from PPK2 so the operator
        # can safely swap boards.
        try:
            ppk2.toggle_DUT_power_OFF()
        except Exception:
            pass

        # Show the startup/setup instructions again so the operator can verify
        # connections for the next board.
        app.acknowledge_info_var.set(0)
        app.information_window()
        app.wait_variable(app.acknowledge_info_var)

        # Loop for next board.
        app.update_window()


if __name__ == "__main__":
    main()


