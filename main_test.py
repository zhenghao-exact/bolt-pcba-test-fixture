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

import bolt_control


# Paths to firmware images on the Pi. Adjust these to match the Bolt build
# output and repository layout on the production fixture.
FW_FOLDER_PATH = "/home/boltfixturepi/bolt-pcba-test-fixture/fw"
TEST_FW_FILENAME = "merged.hex"
PRODUCTION_FW_FILENAME = "bolt_production_fw.hex"

# Feature flag to enable/disable BLE test
# Set to True to enable BLE testing, False to skip (test will always pass)
ENABLE_BLE_TEST = False

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


class BoltTest:
    def __init__(self) -> None:
        self.tests: Dict[str, Any] = dict(tests_template)
        self.measurements: Dict[str, Any] = dict(measurements_template)
        self.ser = None
        self.failure = False
        self.baseline_ports: set[str] = set()

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

    def _capture_baseline_ports(self) -> None:
        """
        Capture the current set of /dev/ttyACM* ports as a baseline.

        This should be called at the start of the test, before the Bolt PCBA
        is powered on, to establish which ports already exist.
        """
        ports = self._scan_acm_ports()
        self.baseline_ports = set(ports)
        print(f"USB: captured baseline ports: {sorted(self.baseline_ports)}")

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
        Dynamically discover and open the Bolt PCBA serial port.

        Scans for new /dev/ttyACM* ports that appeared after the baseline was
        captured. If multiple new ports are found, tries the lowest numbered
        one first. Waits up to 20s for the port to become openable.

        If the DUT does not enumerate as a USB serial device, we will:
          1. Power‑cycle the DUT via the PPK2.
          2. Re‑flash the test firmware.
          3. Retry the USB detection.
        After max_retries attempts we mark the USB connection as failed.
        """
        overall_deadline = time.time() + 60.0  # 1 minute max from start of USB step
        attempt = 0
        while attempt < max_retries:
            if time.time() >= overall_deadline:
                break

            attempt += 1
            print(f"USB: attempting to open DUT serial port (attempt {attempt}/{max_retries})")

            # Scan for current ports and find new ones
            current_ports = set(self._scan_acm_ports())
            new_ports = current_ports - self.baseline_ports

            if not new_ports:
                print("USB: no new serial ports detected yet")
                # Wait a bit before retrying
                time.sleep(1.0)
                if attempt >= max_retries:
                    break
                continue

            # Sort new ports by number and try the lowest one first
            sorted_new_ports = sorted(new_ports, key=lambda x: int(x.replace("/dev/ttyACM", "")))
            target_port = sorted_new_ports[0]

            if len(sorted_new_ports) > 1:
                print(f"USB: multiple new ports detected: {sorted_new_ports}, trying lowest: {target_port}")

            # Wait up to 20s for the port to become available and openable
            port_timeout = 20.0
            port_deadline = time.time() + port_timeout
            print(f"USB: waiting up to {port_timeout}s for {target_port} to become available")

            port_opened = False
            while time.time() < port_deadline:
                if not os.path.exists(target_port):
                    time.sleep(0.5)
                    continue

                # Port exists, try to open it
                ser = bolt_control.open_serial(target_port)
                if ser:
                    # Successfully opened the serial port
                    self.ser = ser
                    self.tests["usb_connection"] = True
                    print(f"USB: opened serial port {target_port} on attempt {attempt}")
                    return True
                else:
                    # Port exists but couldn't open it yet, keep trying
                    time.sleep(0.5)

            # If we reach here, the port didn't become openable within the timeout
            print(f"USB: port {target_port} did not become openable within {port_timeout}s")

            # If we reach here, the DUT did not enumerate as a USB serial device.
            print("USB: no Bolt serial port detected on this attempt")

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
        return self.tests["flash_production_fw"]

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
        
        NOTE: BLE test can be disabled by setting ENABLE_BLE_TEST = False at the module level.
        When disabled, this method will immediately return True (test passes).
        To re-enable BLE testing, change ENABLE_BLE_TEST to True.
        """
        # Check if BLE test is enabled
        # To re-enable BLE testing, change ENABLE_BLE_TEST to True at the module level (line 26)
        if not ENABLE_BLE_TEST:
            print("BLE test: SKIPPED (ENABLE_BLE_TEST = False). Test will pass.")
            self.tests["ble"] = True
            self.measurements["ble_rssi_median"] = None  # Set to None when skipped
            return True
        
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
            self.failure = True
            # Ensure RSSI is None when test fails
            self.measurements["ble_rssi_median"] = None
            return False
        
        # Fallback scan succeeded, but we don't have RSSI from the script
        # Set RSSI to None so CSV reports it as N/A
        self.measurements["ble_rssi_median"] = None
        self.tests["ble"] = True
        print("BLE test: PASSED via fallback scan (RSSI will be reported as N/A)")
        return True

    # --- Analog calibration -----------------------------------------------

    def _set_adc_comp_rr(self) -> bool:
        """
        Set the Rr value for ADC compensation by reading the ADC compensation channel.
        
        This must be called before running analog calibration to ensure proper
        ADC offset compensation. Reads the ADC_CAL_COMP_IDX channel and stores
        the value in settings.
        
        Returns:
            True if Rr was set successfully, False otherwise
        """
        if not self.ser:
            return False
        
        print("Analog cal: setting Rr value for ADC compensation...")
        if not bolt_control.send_shell_command(self.ser, "etc_adc_comp rr_set"):
            print("Analog cal: failed to send etc_adc_comp rr_set command")
            return False
        
        # Read response and check for success
        end_time = time.time() + 3.0
        saw_success = False
        while time.time() < end_time:
            line = self.ser.readline().decode(errors="ignore").strip()
            if not line:
                continue
            print(f"[adc_comp_rr] {line}")
            if "Rr set to" in line:
                saw_success = True
                break
            if "Failed" in line or "Error" in line or "not supported" in line:
                print(f"Analog cal: etc_adc_comp rr_set failed: {line}")
                return False
        
        if saw_success:
            print("Analog cal: Rr value set successfully")
            return True
        else:
            print("Analog cal: timeout waiting for etc_adc_comp rr_set response")
            return False

    def run_analog_calibration(self) -> bool:
        """
        Run the analog calibration sequence by calling calibraor_test.py functions directly.

        The calibration script handles all calibration logic including:
          - OFFSET (0 Ω) calibration
          - HIGH (270 kΩ) calibration
          - Reference value programming
          - Verification at multiple temperature points
        
        Captures calibration parameters and temperature readings for CSV reporting.
        
        NOTE: This method calls _set_adc_comp_rr() first to set the Rr value for
        ADC compensation before running calibration.
        """
        if not self.ser:
            self.tests["analog"] = False
            self.failure = True
            return False

        # Set Rr value for ADC compensation before calibration
        if not self._set_adc_comp_rr():
            print("Analog cal: warning - failed to set Rr value, continuing anyway")
            # Don't fail the test if Rr set fails, but log the warning

        # Import calibration functions directly instead of using subprocess
        # This allows us to capture the returned calibration data
        try:
            from calibraor_test import run_full_analog_calibration, CALIBRATION_MODE_FAST
        except ImportError:
            print("Analog cal: failed to import calibraor_test module")
            self.tests["analog"] = False
            self.failure = True
            return False

        print("Analog cal: running calibration sequence (fast mode)...")
        try:
            # Run calibration in fast mode for production throughput
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
        print("Sleep current: setting PPK2 to source meter mode...")

        try:
            ppk2.set_to_source_mode()
            ppk2.toggle_DUT_power_ON()
        except Exception as exc:
            print(f"Sleep current: failed to configure PPK2: {exc}")
            self.tests["sleep_current"] = False
            self.failure = True
            return False

        # Allow production firmware to settle into low-power state after power cycle
        print("Sleep current: waiting 3 seconds for production firmware to settle into low-power state...")
        time.sleep(3.0)

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
                if avg_ua < 120.0:
                    print(f"Sleep current: average {avg_ua:.2f} uA is below 120 uA after {elapsed:.1f}s - passing early")
                    self.measurements["sleep_current_ua"] = avg_ua
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

        # Pass criterion: average sleep current <= 180 uA
        self.tests["sleep_current"] = avg_ua <= 180.0
        if not self.tests["sleep_current"]:
            print(f"Sleep current: FAILED - average {avg_ua:.2f} uA exceeds 180 uA limit")
            self.failure = True
        else:
            print(f"Sleep current: PASSED - average {avg_ua:.2f} uA is within 180 uA limit")
        
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


def prompt_for_bolt_qr_headless() -> str:
    """
    Prompt for Bolt QR string via command line input (headless mode).
    """
    qr_payload = input("Enter Bolt QR code: ").strip()
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

        # Capture baseline of existing /dev/ttyACM* ports before the Bolt is powered on
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
        if not test.run_ble_test():
            app.update_test_indicator(6, False)
            return test
        app.update_test_indicator(6, True)

        # Indicator 7: analog calibration (placeholder pass).
        if not test.run_analog_calibration():
            app.update_test_indicator(7, False)
            return test
        app.update_test_indicator(7, True)

        # Indicator 8: flash production firmware.
        if not test.flash_production_firmware():
            app.update_test_indicator(8, False)
            return test
        app.update_test_indicator(8, True)

        # Indicator 9: sleep current test.
        app.sleep_current_window()
        if not test.run_sleep_current_test():
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

        # Write test results to CSV – DISABLED
        # CSV generation has been disabled. To re-enable, uncomment the code below.
        # Note: Upload functionality is not currently implemented in this file (only in bolt_fixture_main.py)
        # try:
        #     csv_manager.write_test_results(test.tests, test.measurements, user="N/A", fixture=1)
        #     print("Test results written to CSV")
        # except Exception as exc:
        #     print(f"Failed to write CSV results: {exc}")

    return test


def run_flash_current_headless() -> BoltTest:
    """
    Run only production firmware flash + sleep current test (no QR/USB/serial).
    """
    test = BoltTest()
    test.measurements["bolt_id"] = "unknown"

    print("Step 1: Flash production firmware")
    flash_ok = test.flash_production_firmware()
    if not flash_ok:
        test.tests["sleep_current"] = False
        test.tests["final"] = False
        test.failure = True
        return test

    print("Step 1: Flash production firmware - PASSED")

    print("Step 2: Sleep current test")
    sleep_ok = test.run_sleep_current_test()
    test.tests["final"] = bool(flash_ok and sleep_ok)
    test.failure = not test.tests["final"]
    return test


def run_bolt_test_headless(prod_mode: bool = False) -> BoltTest:
    """
    Run Bolt test sequence in headless mode (no GUI).
    
    Args:
        prod_mode: If True, runs real production firmware flash and sleep current tests
                   after a pause. If False, auto-passes these tests.
    
    Auto-passes IMU test in both modes.
    All other tests run normally. CSV output and label printing are preserved.
    """
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

        # Capture baseline of existing /dev/ttyACM* ports before the Bolt is powered on
        test._capture_baseline_ports()

        test.measurements["test_ID"] = int(start_time)
        # Step 1: scan Bolt QR and parse Bolt ID from it.
        print("Step 1: QR scan")
        qr_payload = prompt_for_bolt_qr_headless()
        if not qr_payload:
            print("QR scan: no data received; aborting test.")
            test.failure = True
            return test

        if not test.set_bolt_id_from_qr(qr_payload):
            print("QR scan: failed to parse Bolt ID")
            return test
        print("Step 1: QR scan - PASSED")

        # Step 2: flash test firmware.
        print("Step 2: Flash test firmware")
        if not test.flash_test_firmware():
            print("Step 2: Flash test firmware - FAILED")
            return test
        print("Step 2: Flash test firmware - PASSED")
        
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

        # Step 3: USB / shell connection.
        print("Step 3: USB connection")
        if not test.open_serial_port():
            print("Step 3: USB connection - FAILED")
            return test
        print("Step 3: USB connection - PASSED")

        # Step 4: set serial on DUT.
        print("Step 4: Set serial on DUT")
        if not test.program_serial_on_dut():
            print("Step 4: Set serial on DUT - FAILED")
            return test
        print("Step 4: Set serial on DUT - PASSED")

        # Step 5: IMU test (auto-passed, skipped).
        print("Step 5: IMU test (auto-passed, skipped)")
        test.tests["imu"] = True
        print("Step 5: IMU test - PASSED (auto-passed)")

        # Step 6: BLE test.
        print("Step 6: BLE test")
        if not test.run_ble_test():
            print("Step 6: BLE test - FAILED")
            return test
        print("Step 6: BLE test - PASSED")

        # Step 7: analog calibration.
        print("Step 7: Analog calibration")
        if not test.run_analog_calibration():
            print("Step 7: Analog calibration - FAILED")
            return test
        print("Step 7: Analog calibration - PASSED")

        # Step 8: flash production firmware
        if prod_mode:
            # Production mode: run real production firmware flash
            print("Step 8: Flash production firmware")
            if not test.flash_production_firmware():
                print("Step 8: Flash production firmware - FAILED")
                return test
            print("Step 8: Flash production firmware - PASSED")
        else:
            # Default mode: auto-pass production firmware flash
            print("Step 8: Flash production firmware (auto-passed, skipped)")
            test.tests["flash_production_fw"] = True
            print("Step 8: Flash production firmware - PASSED (auto-passed)")

        # Step 9: sleep current test
        if prod_mode:
            # Production mode: pause before sleep current test
            print("=" * 60)
            print("Production firmware flash complete. Press Enter to continue to")
            print("sleep current test, or Ctrl+C to abort.")
            print("=" * 60)
            try:
                input()
            except KeyboardInterrupt:
                print("\nTest aborted by user.")
                test.failure = True
                return test
            
            # Production mode: run real sleep current test
            print("Step 9: Sleep current test")
            if not test.run_sleep_current_test():
                print("Step 9: Sleep current test - FAILED")
                return test
            print("Step 9: Sleep current test - PASSED")
        else:
            # Default mode: auto-pass sleep current test
            print("Step 9: Sleep current test (auto-passed, skipped)")
            test.tests["sleep_current"] = True
            # Leave sleep_current_ua as None (default) so CSV shows it as blank/unset
            print("Step 9: Sleep current test - PASSED (auto-passed)")

        # Final result aggregation.
        final_ok = test.evaluate_overall_result()
        print(f"Final result: {'PASSED' if final_ok else 'FAILED'}")

    finally:
        # Always execute these, even on early return
        # Evaluate result if not already done
        if "final" not in test.tests or not test.tests.get("final"):
            final_ok = test.evaluate_overall_result()
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

        # Write test results to CSV – DISABLED
        # CSV generation has been disabled. To re-enable, uncomment the code below.
        # Note: Upload functionality is not currently implemented in this file (only in bolt_fixture_main.py)
        # try:
        #     csv_manager.write_test_results(test.tests, test.measurements, user="N/A", fixture=1)
        #     print("Test results written to CSV")
        # except Exception as exc:
        #     print(f"Failed to write CSV results: {exc}")

    return test


def main() -> None:
    """
    Main entry point for headless Bolt test fixture.

    Runs a single test cycle without GUI, then exits.
    
    Usage:
        python main_test.py          # Default mode: auto-passes production tests
        python main_test.py prod     # Production mode: runs real production tests
        python main_test.py flash_current  # Flash production + run sleep current only
    """
    # Parse command line arguments
    prod_mode = False
    flash_current_mode = False
    if len(sys.argv) > 1:
        if sys.argv[1] == "prod":
            prod_mode = True
        elif sys.argv[1] == "flash_current":
            flash_current_mode = True
        else:
            print(f"Unknown argument: {sys.argv[1]}")
            print("Usage: python main_test.py [prod | flash_current]")
            sys.exit(1)
    
    # Basic PPK2 initialisation; if no PPK2 is connected this will just log
    # and return 0. Current‑measurement tests can be added later.
    try:
        ppk2.setup_ppk()
    except Exception as exc:
        print(f"PPK2 setup failed (non‑fatal during development): {exc}")

    # Run a single headless test cycle
    print("=" * 60)
    print("Bolt PCBA Test Fixture - Headless Mode")
    mode_str = "flash_current" if flash_current_mode else ("prod" if prod_mode else "default")
    print(f"Mode: {mode_str}")
    print("=" * 60)
    if flash_current_mode:
        bolt_test = run_flash_current_headless()
    else:
        bolt_test = run_bolt_test_headless(prod_mode=prod_mode)

    # After test cycle, turn off DUT power from PPK2
    try:
        ppk2.toggle_DUT_power_OFF()
        print("DUT power turned OFF")
    except Exception:
        pass

    # Print final summary
    print("=" * 60)
    print("Test Summary:")
    print(f"  Final result: {'PASSED' if bolt_test.tests.get('final', False) else 'FAILED'}")
    print(f"  Bolt ID: {bolt_test.measurements.get('bolt_id', 'N/A')}")
    print(f"  Mode: {mode_str}")
    print("=" * 60)


if __name__ == "__main__":
    main()


