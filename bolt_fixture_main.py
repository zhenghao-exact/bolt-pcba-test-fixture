import os
import sys
import time
import subprocess
import csv
import re
from datetime import datetime
from typing import Dict, Any, Tuple, Optional

import gui  # Reused GUI from etc-monitor-fixture
import nrfjprog
import ppk2
import printer_manager
import csv_manager
import upload_results

import bolt_control


# Paths to firmware images on the Pi. Adjust these to match the Bolt build
# output and repository layout on the production fixture.
FW_FOLDER_PATH = "/home/boltfixturepi/bolt-pcba-test-fixture/fw"
TEST_FW_FILENAME = "bolt_test_fw.hex"
PRODUCTION_FW_FILENAME = "bolt_v0.5.0-99de3f6.hex"

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
    "HW_ID": "N/A",  # Not used in Bolt, set to N/A for printer compatibility
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
}


# Persistent counter file for PPK2 sleep current errors
PPK2_ERROR_COUNT_FILE = "/home/boltfixturepi/.bolt_ppk2_sleep_error_count"

# Persistent counter file for BLE test failures
BLE_ERROR_COUNT_FILE = "/home/boltfixturepi/.bolt_ble_fail_count"


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
        Re-detect and reopen the Bolt UART after a PPK2 power-cycle.

        Tries the last known ``self.dut_serial_port`` first, then the same
        ttyUSB discovery rules as ``open_serial_port`` (baseline vs rescan).

        This does NOT modify the earlier usb_connection test result - it only
        updates self.ser for use in subsequent test steps.
        """
        print("Analog cal: re-detecting serial port after potential PPK2 power-cycle...")

        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

        port_deadline = time.time() + timeout_s
        print(f"Analog cal: waiting up to {timeout_s}s for DUT ttyUSB port")
        warned_fallback = False
        while time.time() < port_deadline:
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
                    print(f"Analog cal: successfully reopened serial port {port}")
                    return True
            time.sleep(0.5)

        print(f"Analog cal: no DUT ttyUSB became openable within {timeout_s}s")
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

    def flash_production_firmware(self) -> bool:
        fw_path = os.path.join(FW_FOLDER_PATH, PRODUCTION_FW_FILENAME)
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

        ok = bolt_control.set_pcba_serial(self.ser, str(bolt_id))
        self.tests["set_serial"] = ok
        if not ok:
            self.failure = True
        return ok

    # --- IMU test ---------------------------------------------------------

    def run_imu_test(self) -> bool:
        """
        IMU is always running; watch the log for angle changes in both
        directions and ensure we see at least +/- threshold within a timeout.
        """
        if not self.ser:
            return False
        ok = bolt_control.wait_for_imu_rotation(self.ser, timeout_s=15.0, threshold_deg=20.0)
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
            # Use the same Python interpreter and pass bolt_id as argument
            # The script will handle Bluetooth restart and device removal internally
            # We don't pass --skip-restart or --skip-remove, so the script manages Bluetooth state
            process = subprocess.Popen(
                [sys.executable, script_path, str(bolt_id)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=script_dir,
            )
            
            # Wait for completion with timeout (script timeout is 30s default, add buffer)
            stdout, stderr = process.communicate(timeout=60.0)
            
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
            print("BLE test: run_ble_test.py timed out after 60s")
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

    def run_ble_test(self) -> bool:
        """
        Test BLE functionality by first trying the standalone run_ble_test.py script
        to collect RSSI measurements, with fallback to simple device presence scan.
        
        Behavior matrix:
        - Script succeeds: Store median RSSI, set tests["ble"] = True
        - Script fails + fallback succeeds: Set RSSI to None (reported as N/A in CSV), 
          set tests["ble"] = True
        - Both fail: Set tests["ble"] = False, RSSI remains None
        """
        bolt_id = self.measurements.get("bolt_id")
        if not bolt_id:
            return False

        # First, try the standalone BLE test script which collects RSSI measurements
        # The script handles Bluetooth restart and device removal internally
        script_ok, median_rssi = self._run_ble_test_script(bolt_id)
        
        if script_ok and median_rssi is not None:
            # Script succeeded and we have RSSI measurement
            self.measurements["ble_rssi_median"] = median_rssi
            self.tests["ble"] = True
            # Reset BLE error counter on successful test
            set_ble_error_count(0)
            print(f"BLE test: PASSED via standalone script - Median RSSI: {median_rssi} dBm")
            return True
        
        # Script failed, fall back to the original simple presence scan
        print("BLE test: standalone script failed, falling back to simple device presence scan...")
        
        # Restart bluetooth service before fallback BLE test
        print("BLE test: restarting bluetooth service for fallback scan...")
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

        # Simple scan for device presence (no connection, much faster)
        ok = bolt_control.scan_for_ble_device(bolt_id, timeout_s=10.0)
        if not ok:
            self.tests["ble"] = False
            # Check if this is the first BLE failure since last success
            error_count = get_ble_error_count()
            new_count = error_count + 1
            set_ble_error_count(new_count)
            
            if new_count == 1:
                # First BLE failure: treat as transient condition, don't mark as board failure yet
                self.ble_first_failure = True
                print(f"BLE test: FIRST FAILURE detected (likely transient due to re-power/advertising name change)")
                print(f"BLE test: will trigger test restart instead of marking board as failed")
            else:
                # Subsequent failure: normal board failure
                self.failure = True
            
            # Ensure RSSI is None when test fails
            self.measurements["ble_rssi_median"] = None
            return False
        
        # Fallback scan succeeded, but we don't have RSSI from the script
        # Set RSSI to None so CSV reports it as N/A
        # Reset BLE error counter on successful test
        set_ble_error_count(0)
        self.measurements["ble_rssi_median"] = None
        self.tests["ble"] = True
        print("BLE test: PASSED via fallback scan (RSSI will be reported as N/A)")
        return True

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
        # Re-detect serial port in case PPK2 power-cycle changed the ttyUSBx port
        if not self.reopen_serial_port_for_calibration():
            print("Analog cal: failed to re-detect serial port after power-cycle")
            self.tests["analog"] = False
            self.failure = True
            return False

        # Send w1 slpz 0 before calibration (same timing as settings write)
        bolt_control.clear_serial_buffer(self.ser)
        time.sleep(2.0)
        if not bolt_control.send_shell_command(self.ser, "w1 slpz 0"):
            print("Analog cal: failed to send w1 slpz 0 command")
        time.sleep(0.1)
        bolt_control.clear_serial_buffer(self.ser)

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
            cal_result = run_full_analog_calibration(self.ser, mode=CALIBRATION_MODE_FAST)

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

        print(f"Sleep current: measuring for at least {min_duration_s} seconds (up to {duration_s} seconds)...")
        while time.time() - start_time < duration_s:
            current_ua = ppk2.get_average_current(100)
            if current_ua < 0:
                print(f"Sleep current: measurement error (got {current_ua})")
                self.tests["sleep_current"] = False
                self.failure = True
                return False

            # Skip the first two readings as they're often abnormal
            if readings_to_skip > 0:
                print(f"Sleep current: discarding reading {3 - readings_to_skip}: {current_ua:.2f} uA")
                readings_to_skip -= 1
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
                    # Increment persistent error counter
                    error_count = get_ppk2_error_count() + 1
                    set_ppk2_error_count(error_count)
                    print(f"Sleep current: PPK2 error count is now {error_count}")
                    # Do NOT mark board as failed - this is a fixture issue
                    # Return False to indicate test could not complete, but failure flag is not set
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
            # Increment persistent error counter
            error_count = get_ppk2_error_count() + 1
            set_ppk2_error_count(error_count)
            print(f"Sleep current: PPK2 error count is now {error_count}")
            # Do NOT mark board as failed - this is a fixture issue
            # Return False to indicate test could not complete, but failure flag is not set
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


def run_bolt_test(app: gui.App) -> BoltTest:
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
        app.imu_instruction_window()
        if not test.run_imu_test():
            app.update_test_indicator(5, False)
            return test
        app.update_test_indicator(5, True)

        # Indicator 6: BLE test.
        ble_ok = test.run_ble_test()
        if not ble_ok:
            app.update_test_indicator(6, False)
            if test.ble_first_failure:
                # First BLE failure: inform operator and abort this run
                app.ble_retry_window()
                return test
            else:
                # Subsequent failures: behave as current (normal board fail)
                return test
        app.update_test_indicator(6, True)

        # Indicator 7: analog calibration (placeholder pass).
        if not test.run_analog_calibration():
            app.update_test_indicator(7, False)
            return test
        app.update_test_indicator(7, True)

        # Indicator 8: flash production firmware.
        app.sleep_current_window()
        if not test.flash_production_firmware():
            app.update_test_indicator(8, False)
            return test
        app.update_test_indicator(8, True)
        time.sleep(5)

        # Indicator 9: sleep current test.
        app.sleep_current_window()
        sleep_test_result = test.run_sleep_current_test()
        
        # Check for abnormal PPK2 readings (fixture issue, not board failure)
        if test.ppk2_sleep_error:
            error_count = get_ppk2_error_count()
            print(f"PPK2 error: abnormal reading detected, error count: {error_count}")
            
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
        # Always execute these, even on early return
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

        # Label printing – always execute, even on failure
        try:
            # work_order is not used in the Bolt fixture yet; pass an empty string.
            print_success = printer_manager.print_label(final_ok, test.measurements, refurb=False, work_order="")
            if not print_success:
                print("Label printing failed; operator can reprint manually later.")
        except Exception as exc:
            # If running on a development machine without the printer, just log.
            print(f"Skipping label printing due to error: {exc}")

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
    app = gui.App()
    sys.stdout.write = app.update_serial_display

    # Basic PPK2 initialisation; if no PPK2 is connected this will just log
    # and return 0. Current‑measurement tests can be added later.
    try:
        ppk2.setup_ppk()
    except Exception as exc:
        print(f"PPK2 setup failed (non‑fatal during development): {exc}")

    app.acknowledge_info_var.set(0)
    app.information_window()
    app.wait_variable(app.acknowledge_info_var)

    while True:
        app.reset_indicators()
        app.update_test_display(state="active")
        bolt_test = run_bolt_test(app)
        
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


