import re
import time
from typing import Tuple, Optional, List

from serial import Serial, SerialException  # type: ignore[import-not-found]
from bleak import BleakScanner, BleakClient  # type: ignore[import-not-found]
from bleak.exc import BleakError  # type: ignore[import-not-found]
try:
    from bleak.backends.scanner import AdvertisementData  # type: ignore[import-not-found]
except ImportError:
    # Fallback for different Bleak versions - use Any type
    AdvertisementData = None  # type: ignore
import asyncio

BAUDRATE = 115200
BLE_CHAR_UUID = "4a7b9d11-2235-47ae-b2c1-1559361c6a95"


def open_serial(port: str, timeout: float = 0.5) -> Optional[Serial]:
    """
    Open a serial connection to the Bolt DUT.

    This is intentionally minimal; the fixture will typically talk to a single
    Bolt board connected over USB CDC ACM.
    
    Attempts to open at BAUDRATE, but falls back to 115200 if the higher rate
    is not supported by the USB CDC ACM driver.
    """
    try:
        ser = Serial(port, BAUDRATE, timeout=timeout)
        print(f"[serial] Opened {port} at {BAUDRATE} baud")
        return ser
    except (SerialException, OSError, IOError) as exc:
        # If higher baud rate fails, try 115200 as fallback (widely supported)
        if BAUDRATE > 115200:
            print(f"[serial] Failed to open {port} at {BAUDRATE} baud: {exc}")
            print(f"[serial] Trying fallback baud rate 115200...")
            try:
                ser = Serial(port, 115200, timeout=timeout)
                print(f"[serial] Opened {port} at 115200 baud (fallback)")
                return ser
            except (SerialException, OSError, IOError) as exc2:
                print(f"[serial] Fallback to 115200 also failed: {exc2}")
                return None
        print(f"[serial] Failed to open {port} at {BAUDRATE} baud: {exc}")
        return None


def ensure_serial_baudrate(ser: Serial, expected_baudrate: int = BAUDRATE) -> bool:
    """
    Verify that an existing serial connection is using the expected baud rate.
    
    If the connection is at a different baud rate, attempt to change it.
    Note: Changing baud rate on an open connection may not work reliably with
    USB CDC ACM, so this is mainly for verification/logging.
    
    Returns True if the connection is at the expected rate (or was successfully
    changed), False otherwise.
    """
    if not ser or not ser.is_open:
        return False
    
    current_baudrate = ser.baudrate
    if current_baudrate == expected_baudrate:
        return True
    
    print(f"[serial] Connection baud rate is {current_baudrate}, expected {expected_baudrate}")
    try:
        ser.baudrate = expected_baudrate
        print(f"[serial] Changed baud rate to {expected_baudrate}")
        return True
    except (SerialException, OSError, IOError) as exc:
        print(f"[serial] Failed to change baud rate: {exc}")
        return False


def send_shell_command(ser: Serial, cmd: str) -> bool:
    """
    Send a Zephyr shell command and return True if the write succeeded.

    The caller is responsible for reading / parsing the response.
    Uses flush to ensure the command is fully transmitted before returning.
    """
    try:
        if not cmd.endswith("\n"):
            cmd = cmd + "\n"
        ser.write(cmd.encode())
        ser.flushOutput()
        return True
    except (SerialException, OSError, IOError) as exc:
        print(f"[serial] Failed to send command '{cmd.strip()}': {exc}")
        return False


def wait_for_prompt(ser: Serial, timeout_s: float = 2.0) -> bool:
    """
    Drain the serial buffer and wait until the Zephyr shell prompt appears.

    The exact prompt string can be customised later; for now we accept any line
    ending with a typical Zephyr shell prompt marker (e.g. 'uart:~$').
    """
    end_time = time.time() + timeout_s
    while time.time() < end_time:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        if line.endswith("uart:~$") or line.endswith("shell:~$"):
            return True
    return False


def clear_serial_buffer(ser: Serial) -> None:
    """Read and discard any pending data from the serial buffer."""
    ser.timeout = 0
    try:
        while True:
            line = ser.readline()
            if not line:
                break
    finally:
        ser.timeout = 0.5


# --- ADC calibration helpers -------------------------------------------------


def _read_lines_until(ser: Serial, timeout_s: float) -> List[str]:
    """
    Read and return all non-empty lines from the serial port until timeout.
    """
    end_time = time.time() + timeout_s
    lines: List[str] = []
    while time.time() < end_time:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        lines.append(line)
    return lines


def adc_sample_raw_once(ser: Serial, timeout_s: float = 3.0) -> Optional[int]:
    """
    Trigger a single raw ADC sample on the Bolt and parse the returned value.

    Uses the `etc_adc sample_raw` shell command implemented in etc_adc_shell.c,
    which prints e.g.:
        Raw ADC value: 1234
    """
    if not send_shell_command(ser, "etc_adc sample_raw"):
        return None

    end_time = time.time() + timeout_s
    while time.time() < end_time:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        print(f"[adc_raw] {line}")
        if "Failed to read raw value" in line or "Error" in line:
            return None
        if "Raw ADC value:" in line:
            m = re.search(r"(-?\d+)", line)
            if m:
                try:
                    return int(m.group(1))
                except ValueError:
                    return None
    return None


def adc_sample_raw_average(
    ser: Serial,
    samples: int = 10,
    discard: int = 2,
    timeout_per_sample_s: float = 3.0,
) -> Optional[float]:
    """
    Take multiple raw ADC samples and return the average of the last `samples`
    values after discarding an initial number of readings.
    """
    values: List[int] = []

    # Optionally discard a few initial samples to allow the pipeline to settle.
    for _ in range(discard):
        _ = adc_sample_raw_once(ser, timeout_s=timeout_per_sample_s)

    for _ in range(samples):
        val = adc_sample_raw_once(ser, timeout_s=timeout_per_sample_s)
        if val is None:
            return None
        values.append(val)

    if not values:
        return None
    return sum(values) / float(len(values))


def adc_sample_calibrated_once(ser: Serial, timeout_s: float = 4.0) -> Optional[float]:
    """
    Trigger a single calibrated ADC sample on the Bolt and parse the
    temperature in degrees Celsius from the `etc_adc sample` shell command.

    The firmware prints:
      Factory: offset=... high=... ref=...
      User:    offset=... high=... ref=...
      Raw ADC value: <int>
      Calibrated value: <float>
      ...
    """
    if not send_shell_command(ser, "etc_adc sample"):
        return None

    end_time = time.time() + timeout_s
    cal_val: Optional[float] = None
    while time.time() < end_time:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        print(f"[adc_sample] {line}")
        if "Calibrated value:" in line:
            m = re.search(r"([-0-9.]+)", line.split("Calibrated value:")[-1])
            if m:
                try:
                    cal_val = float(m.group(1))
                except ValueError:
                    return None
        if "Failed to apply calibration" in line or "Error" in line:
            return None
    return cal_val


def adc_sample_calibrated_average(
    ser: Serial,
    samples: int = 3,
    discard: int = 1,
    timeout_per_sample_s: float = 4.0,
) -> Optional[float]:
    """
    Take multiple calibrated ADC samples and return the average calibrated
    temperature value in degrees Celsius.
    """
    values: List[float] = []

    for _ in range(discard):
        _ = adc_sample_calibrated_once(ser, timeout_s=timeout_per_sample_s)

    for _ in range(samples):
        val = adc_sample_calibrated_once(ser, timeout_s=timeout_per_sample_s)
        if val is None:
            return None
        values.append(val)

    if not values:
        return None
    return sum(values) / float(len(values))


def _write_adc_param(ser: Serial, subcmd: str, value: float, scope: str = "factory") -> bool:
    """
    Generic helper to write an ADC calibration parameter via etc_adc shell.

    subcmd: 'offset', 'high', or 'ref'
    """
    cmd = f"etc_adc {subcmd} {value:.3f} {scope}"
    if not send_shell_command(ser, cmd):
        return False

    end_time = time.time() + 4.0
    saw_ok = False
    while time.time() < end_time:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        print(f"[adc_{subcmd}] {line}")
        if "Failed to set" in line or "Usage:" in line or "Invalid scope" in line:
            return False
        if "OK." in line and subcmd in line:
            saw_ok = True
            break
    return saw_ok


def write_adc_offset_factory(ser: Serial, offset: float) -> bool:
    return _write_adc_param(ser, "offset", offset, scope="factory")


def write_adc_high_factory(ser: Serial, high: float) -> bool:
    return _write_adc_param(ser, "high", high, scope="factory")


def write_adc_ref_factory(ser: Serial, ref: float) -> bool:
    return _write_adc_param(ser, "ref", ref, scope="factory")


def parse_bolt_id_from_qr(qr_payload: str) -> Optional[str]:
    """
    Extract the Bolt ID from a scanned QR payload.

    The expected format is: https://exacttechnology.com/qr/d-30000080
    This function returns the numeric portion (e.g. '30000080') or None if
    parsing fails.
    """
    # Look for the last sequence of digits in the string, preferably after 'd-'
    match = re.search(r"d-(\d+)", qr_payload)
    if match:
        return match.group(1)

    # Fallback: any trailing integer
    match = re.search(r"(\d+)$", qr_payload)
    if match:
        return match.group(1)

    return None


def _bolt_id_to_le_hex(bolt_id: str) -> Optional[str]:
    """
    Convert a Bolt ID (e.g. '30000080') to the little‑endian hex format expected
    by the firmware `settings write sn/id` command.

    Example:
        '30000080' (decimal) -> 0x01C9C3D0 -> 'D0C3C901'
    """
    try:
        value = int(bolt_id)
    except ValueError:
        return None

    if value < 0 or value > 0xFFFFFFFF:
        return None

    be_hex = f"{value:08X}"  # big‑endian: e.g. '01C9C3D0'
    bytes_be = [be_hex[i : i + 2] for i in range(0, 8, 2)]
    bytes_le = list(reversed(bytes_be))
    return "".join(bytes_le)


def set_pcba_serial(ser: Serial, bolt_id: str) -> bool:
    """
    Set the Bolt PCBA serial number on the DUT using the Zephyr settings shell.

    The firmware expects a 32‑bit value encoded in little‑endian hex:

        settings write sn/id <little-endian-hex>

    For example, Bolt ID '30000080' becomes `settings write sn/id D0C3C901`.
    """
    clear_serial_buffer(ser)

    le_hex = _bolt_id_to_le_hex(bolt_id)
    if le_hex is None:
        print(f"Invalid Bolt ID for serial write: {bolt_id}")
        return False

    time.sleep(2.0)

    # Match the manual minicom usage exactly: leading newline, command, newline.
    cmd = f"\nsettings write sn/id {le_hex}\n"
    try:
        ser.write(cmd.encode())
        ser.flushOutput()
        time.sleep(0.1)
    except SerialException:
        print("Failed to send settings write command over serial.")
        return False

    # Read lines for a short period and look for any sign of success or error.
    end_time = time.time() + 3.0
    saw_ok = False
    while time.time() < end_time:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        # For debugging, echo what we saw.
        print(f"[set_pcba_serial] {line}")
        if "Error" in line or "FAIL" in line.upper():
            return False
        if "OK" in line or "sn/id" in line:
            saw_ok = True
    return saw_ok


def simple_health_check(ser: Serial) -> bool:
    """
    Lightweight placeholder for a DUT health check.

    This can later be replaced with a call into a custom Bolt shell command
    that runs a self‑test on the device.
    """
    clear_serial_buffer(ser)
    if not send_shell_command(ser, "help"):
        return False

    # Expect that 'help' prints something and returns to the prompt.
    end_time = time.time() + 2.0
    saw_output = False
    while time.time() < end_time:
        line = ser.readline().decode(errors="ignore")
        if line:
            saw_output = True
        if "uart:~$" in line or "shell:~$" in line:
            return saw_output
    return False


def wait_for_imu_rotation(ser: Serial, timeout_s: float = 15.0, threshold_deg: float = 20.0) -> bool:
    """
    Watch the UART log for IMU angle messages and ensure we see movement in
    both directions of at least +/- threshold_deg within timeout_s.

    We look for lines like:
        "imu: etc_imu_mlc_angle_fetch: new angle: -22.5"
    """
    clear_serial_buffer(ser)
    end_time = time.time() + timeout_s
    min_angle = 999.0
    max_angle = -999.0

    while time.time() < end_time:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue

        if "new angle:" in line:
            m = re.search(r"new angle:\s*([-0-9.]+)", line)
            if not m:
                continue
            try:
                angle = float(m.group(1))
            except ValueError:
                continue

            min_angle = min(min_angle, angle)
            max_angle = max(max_angle, angle)
            print(f"[imu] angle={angle} min={min_angle} max={max_angle}")

            if min_angle <= -threshold_deg and max_angle >= threshold_deg:
                return True

    return False


async def _ble_connect_and_read_rssi_async(
    target_name: str,
    char_uuid: str,
    min_samples: int,
    timeout_s: float,
):
    """
    Resolve the Bolt MAC by scanning for advertisements matching target_name,
    then connect over GATT and subscribe to the given characteristic UUID.

    Returns a list of RSSI samples (from the custom payload's trailing RSSI
    byte when present and valid).
    
    NOTE: This function connects to the device and reads RSSI from GATT notification
    payloads. For advertisement-only RSSI collection (without connecting), use
    `_scan_ble_advertisement_rssi_async()` instead. The standalone BLE test script
    (`run_ble_test.py`) uses advertisement-only scanning and does not call this function.
    """
    time.sleep(15.0)
    print(f"BLE: scanning for device named '{target_name}'")
    end_time_scan = time.time() + timeout_s
    target = None

    # Perform repeated short scans until the device is found or timeout is hit.
    while time.time() < end_time_scan and target is None:
        devices = await BleakScanner().discover(timeout=3.0)
        for d in devices:
            if d.name == target_name:
                target = d
                break
        if target is None:
            print("BLE: scan iteration finished, device not found yet")
            await asyncio.sleep(0.5)

    if target is None:
        print(f"BLE: device '{target_name}' not found during scan window (~{timeout_s}s)")
        return []

    address = target.address
    print(f"BLE: found {target_name} at {address} (adv RSSI={getattr(target, 'rssi', 'N/A')})")

    client = None
    samples = []

    async def _rescan_device() -> Optional[str]:
        """
        Re-scan for the device and return its address, or None if not found.
        """
        print(f"BLE: re-scanning for device '{target_name}'...")
        try:
            devices = await BleakScanner().discover(timeout=5.0)
            for d in devices:
                if d.name == target_name:
                    new_address = d.address
                    print(f"BLE: re-found {target_name} at {new_address}")
                    return new_address
            print(f"BLE: device '{target_name}' not found in re-scan")
            return None
        except Exception as exc:
            print(f"BLE: error during re-scan: {exc}")
            return None

    async def _connect_with_retries(max_attempts: int = 3) -> bool:
        """
        Try to connect to the device a few times, handling common BlueZ transient errors.
        Re-scans for the device between retries if connection fails.
        """
        nonlocal client, address
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            
            # Disconnect any existing client before retrying
            if client is not None:
                try:
                    if client.is_connected:
                        await client.disconnect()
                except Exception:
                    pass
                client = None
            
            # Re-scan for the device if this is a retry (not the first attempt)
            if attempt > 1:
                new_address = await _rescan_device()
                if new_address is None:
                    print(f"BLE: device not found in re-scan, waiting before retry...")
                    await asyncio.sleep(2.0)
                    new_address = await _rescan_device()
                    if new_address is None:
                        print(f"BLE: device still not found after second re-scan attempt")
                        if attempt < max_attempts:
                            await asyncio.sleep(2.0)
                        continue
                address = new_address
            
            # Create a new client for this attempt
            client = BleakClient(address)
            
            try:
                # Use longer timeout for first attempt, shorter for retries
                timeout = 60.0 if attempt == 1 else 45.0
                print(f"BLE: connecting to {address} (attempt {attempt}/{max_attempts}, timeout={timeout}s)")
                await client.connect(timeout=timeout)
                if client.is_connected:
                    print(f"BLE: connected to {address} on attempt {attempt}")
                    return True
                print(f"BLE: connect attempt {attempt} completed but client.is_connected is False")
            except Exception as exc:
                # BlueZ sometimes reports org.bluez.Error.InProgress when another connect is ongoing
                if "InProgress" in repr(exc):
                    print("BLE: connect() returned InProgress, another connection attempt is active; backing off")
                    await asyncio.sleep(1.0)
                elif "TimeoutError" in type(exc).__name__:
                    print(f"BLE: connect attempt {attempt} timed out after {timeout}s")
                else:
                    print(f"BLE: connect attempt {attempt} failed with error: {exc!r}")
            
            # Backoff between attempts (longer wait for retries)
            if attempt < max_attempts:
                wait_time = 2.0 if attempt == 1 else 3.0
                print(f"BLE: waiting {wait_time}s before next connection attempt...")
                await asyncio.sleep(wait_time)
        
        return bool(client and client.is_connected)

    async def _start_notify_with_retry(max_attempts: int = 2) -> bool:
        """
        Start notifications on the given characteristic with a small retry window
        for transient ATT/BlueZ errors.
        """
        if client is None or not client.is_connected:
            print("BLE: client is not connected; cannot start notifications")
            return False
        
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            try:
                print(f"BLE: subscribing to {char_uuid} (attempt {attempt}/{max_attempts})")
                await client.start_notify(char_uuid, notification_handler)
                print(f"BLE: subscribed to {char_uuid}")
                return True
            except Exception as exc:
                msg = repr(exc)
                print(f"BLE: start_notify error on attempt {attempt}: {msg}")
                # Mirror the robustness in thingsboard.device: treat "Unlikely" / ATT 0x0E as transient
                if ("Unlikely" in msg or "0x0e" in msg.lower()) and attempt < max_attempts:
                    print("BLE: treating ATT 'Unlikely error' as transient; retrying shortly")
                    await asyncio.sleep(0.5)
                    continue
                # For other errors, do not keep retrying endlessly
                if attempt >= max_attempts:
                    print("BLE: giving up on start_notify after max attempts")
                    return False
        return False

    async def notification_handler(sender: int, data: bytearray):
        # data is the same payload parsed in BoltBLEUplinkConverter
        raw = bytes(data)
        print(f"BLE notify: len={len(raw)} payload={raw.hex()}")
        if len(raw) < 18:
            return

        # Mirror BoltBLEUplinkConverter's tail parsing:
        # base 18 bytes, then optional RSSI (1) and optional PCBA temp (2).
        offset = 18
        remaining = len(raw) - offset
        rssi_dbm = None

        if remaining >= 3:
            # [RSSI][PCBA_temp_L][PCBA_temp_H]
            rssi_b = raw[offset : offset + 1]
            rssi_val = int.from_bytes(rssi_b, byteorder="little", signed=True)
            if rssi_val != -128:
                rssi_dbm = int(rssi_val)
        elif remaining == 1:
            # Legacy 19-byte payload: only RSSI present
            rssi_b = raw[offset : offset + 1]
            rssi_val = int.from_bytes(rssi_b, byteorder="little", signed=True)
            if rssi_val != -128:
                rssi_dbm = int(rssi_val)

        if rssi_dbm is not None:
            samples.append(rssi_dbm)
            print(f"BLE parsed RSSI: {rssi_dbm} dBm (samples={samples})")

    try:
        if not await _connect_with_retries():
            print("BLE: failed to connect after retries; aborting BLE RSSI test")
            return []

        if client is None or not client.is_connected:
            print("BLE: client is not connected; aborting BLE RSSI test")
            return []

        if not await _start_notify_with_retry():
            print("BLE: failed to enable notifications; aborting BLE RSSI test")
            return []

        end_time = time.time() + timeout_s
        while time.time() < end_time and len(samples) < min_samples:
            # Wait for notifications to arrive; log progress occasionally
            await asyncio.sleep(0.5)
            if len(samples) > 0:
                print(f"BLE: waiting for more RSSI samples ({len(samples)}/{min_samples})")

        if len(samples) < min_samples:
            print(
                f"BLE: timeout waiting for RSSI samples "
                f"({len(samples)} collected, {min_samples} required)"
            )

        if client and client.is_connected:
            await client.stop_notify(char_uuid)
    except Exception as exc:
        print(f"BLE: error during connect/read: {exc}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return samples


async def _scan_for_device_presence_async(target_name: str, timeout_s: float = 10.0) -> bool:
    """
    Simple scan for a BLE device by name - just checks if device is advertising.
    No RSSI collection, no samples needed - just presence detection.
    
    Returns True if the device is found advertising, False otherwise.
    """
    print(f"BLE: scanning for device named '{target_name}'")
    end_time = time.time() + timeout_s
    device_found = False
    device_address = None
    
    def detection_callback(device, advertisement_data):
        """Callback for when a device is detected during scanning."""
        nonlocal device_found, device_address
        
        if device.name == target_name:
            device_found = True
            device_address = device.address
            rssi = None
            if hasattr(advertisement_data, 'rssi'):
                rssi = advertisement_data.rssi
            if rssi is None:
                rssi = getattr(device, 'rssi', 'N/A')
            print(f"BLE: found {target_name} at {device_address} (RSSI={rssi})")
    
    scanner = BleakScanner(detection_callback=detection_callback)
    
    try:
        # Start scanning
        await scanner.start()
        
        # Continue scanning until device is found or timeout
        while time.time() < end_time and not device_found:
            await asyncio.sleep(0.5)
        
        # Stop scanning
        await scanner.stop()
    except Exception as exc:
        print(f"BLE: scan error: {exc}")
        try:
            await scanner.stop()
        except Exception:
            pass
    
    if device_found:
        return True
    
    print(f"BLE: device '{target_name}' not found during scan window (~{timeout_s}s)")
    return False


async def _scan_ble_advertisement_rssi_async(
    target_name: str,
    min_samples: int,
    timeout_s: float,
) -> List[int]:
    """
    Scan for BLE device advertisements and collect RSSI samples from advertisement packets.
    
    This function does NOT connect to the device. It only collects RSSI values from
    advertisement packets seen during scanning.
    
    Args:
        target_name: The device name to scan for (e.g., 'Bolt_30000080')
        min_samples: Minimum number of RSSI samples to collect
        timeout_s: Maximum time to spend scanning
    
    Returns:
        List of RSSI samples (in dBm) collected from advertisements
    """
    print(f"BLE: scanning for device '{target_name}' to collect advertisement RSSI samples")
    end_time = time.time() + timeout_s
    samples: List[int] = []
    last_seen_address = None
    devices_seen_count = 0
    bolt_devices_seen = []
    
    def detection_callback(device, advertisement_data):
        """Callback for when a device is detected during scanning."""
        nonlocal samples, last_seen_address, devices_seen_count, bolt_devices_seen
        
        devices_seen_count += 1
        device_name = device.name if device.name else "(no name)"
        address = device.address
        
        # Track all Bolt devices seen
        if "Bolt" in device_name:
            if device_name not in bolt_devices_seen:
                bolt_devices_seen.append(device_name)
                rssi = None
                if hasattr(advertisement_data, 'rssi'):
                    rssi = advertisement_data.rssi
                if rssi is None:
                    rssi = getattr(device, 'rssi', 'N/A')
                print(f"BLE: ⚠ Found Bolt device: name='{device_name}' address={address} RSSI={rssi} (looking for '{target_name}')")
        
        # Log first 10 devices for debugging
        if devices_seen_count <= 10:
            rssi = None
            if hasattr(advertisement_data, 'rssi'):
                rssi = advertisement_data.rssi
            if rssi is None:
                rssi = getattr(device, 'rssi', 'N/A')
            print(f"BLE: detected device #{devices_seen_count}: name='{device_name}' address={address} RSSI={rssi}")
        
        if device.name == target_name:
            # RSSI is available in the AdvertisementData object (if available)
            rssi = None
            if hasattr(advertisement_data, 'rssi'):
                rssi = advertisement_data.rssi
            
            # Fallback: try to get RSSI from device object if not in advertisement_data
            if rssi is None:
                rssi = getattr(device, 'rssi', None)
            
            # Always add a sample when device is found, even if RSSI is not available
            # Use RSSI if available, otherwise use a placeholder value (0) to indicate device was seen
            if rssi is not None:
                try:
                    rssi_int = int(rssi)
                    samples.append(rssi_int)
                    print(f"BLE: ✓ MATCH! collected adv RSSI={rssi_int} dBm from {address} (samples={len(samples)})")
                except (ValueError, TypeError):
                    # If RSSI conversion fails, still count as a sample with placeholder
                    samples.append(0)
                    print(f"BLE: ✓ MATCH! found {target_name} at {address} (RSSI unavailable, samples={len(samples)})")
            else:
                # Device found but RSSI not available - still count as a sample
                samples.append(0)
                print(f"BLE: ✓ MATCH! found {target_name} at {address} (RSSI not available, samples={len(samples)})")
            
            # Track the address we're seeing (for logging purposes)
            if last_seen_address is None:
                last_seen_address = address
        elif device.name and "Bolt" in device.name:
            # Log any Bolt device that doesn't match (for debugging)
            print(f"BLE: ⚠ Found Bolt device but name mismatch: '{device.name}' != '{target_name}'")
    
    scanner = BleakScanner(detection_callback=detection_callback)
    
    try:
        # Start scanning
        await scanner.start()
        
        # Continue scanning until we have enough samples or timeout
        while time.time() < end_time and len(samples) < min_samples:
            await asyncio.sleep(0.5)
        
        # Stop scanning
        await scanner.stop()
    except Exception as exc:
        print(f"BLE: scan error while collecting RSSI: {exc}")
        try:
            await scanner.stop()
        except Exception:
            pass
    
    if len(samples) < min_samples:
        print(
            f"BLE: collected {len(samples)} advertisement RSSI samples "
            f"(target: {min_samples}) within {timeout_s}s timeout"
        )
        print(f"BLE: Total devices detected during scan: {devices_seen_count}")
        if bolt_devices_seen:
            print(f"BLE: Bolt devices seen (but not matching '{target_name}'): {bolt_devices_seen}")
        else:
            print(f"BLE: No Bolt devices detected at all during scan")
    else:
        print(f"BLE: collected {len(samples)} advertisement RSSI samples")
    
    return samples


def scan_for_ble_device(bolt_id: str, timeout_s: float = 10.0) -> bool:
    """
    Simple scan for a Bolt BLE device by ID without connecting.
    
    This is a fast check that only verifies the device is advertising.
    No GATT connection is made. No RSSI collection - just presence detection.
    
    Args:
        bolt_id: The Bolt device ID to scan for (e.g., '30000080')
        timeout_s: Maximum time to spend scanning (default: 10.0 seconds)
    
    Returns:
        True if the device is found advertising, False otherwise.
    """
    target_name = f"Bolt_{bolt_id}"
    try:
        return asyncio.run(_scan_for_device_presence_async(target_name, timeout_s))
    except BleakError as exc:
        print(f"BLE scan: BleakError while scanning: {exc}")
        print(
            "BLE scan: ensure the Raspberry Pi Bluetooth adapter is present, "
            "powered, and not blocked (e.g. check 'rfkill list' and "
            "'systemctl status bluetooth')."
        )
        return False
    except RuntimeError:
        # If we're already in an event loop, create a dedicated one.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(_scan_for_device_presence_async(target_name, timeout_s))
        except BleakError as exc:
            print(f"BLE scan (loop): BleakError while scanning: {exc}")
            print(
                "BLE scan: ensure the Raspberry Pi Bluetooth adapter is present, "
                "powered, and not blocked (e.g. check 'rfkill list' and "
                "'systemctl status bluetooth')."
            )
            loop.close()
            return False
        loop.close()
        return result


def scan_ble_rssi(bolt_id: str, min_samples: int = 3, timeout_s: float = 30.0):
    """
    Connect to the Bolt BLE device whose name includes the Bolt ID
    (e.g. 'Bolt_30000080'), subscribe to its telemetry characteristic, and
    compute the median RSSI from at least min_samples payloads.

    Returns (ok, median_rssi).
    """
    target_name = f"Bolt_{bolt_id}"
    try:
        samples = asyncio.run(
            _ble_connect_and_read_rssi_async(target_name, BLE_CHAR_UUID, min_samples, timeout_s)
        )
    except BleakError as exc:
        # Common case on fixtures without a working Bluetooth adapter.
        print(f"BLE scan: BleakError while scanning/connecting: {exc}")
        print(
            "BLE scan: ensure the Raspberry Pi Bluetooth adapter is present, "
            "powered, and not blocked (e.g. check 'rfkill list' and "
            "'systemctl status bluetooth')."
        )
        return False, None
    except RuntimeError:
        # If we're already in an event loop, create a dedicated one.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            samples = loop.run_until_complete(
                _ble_connect_and_read_rssi_async(target_name, BLE_CHAR_UUID, min_samples, timeout_s)
            )
        except BleakError as exc:
            print(f"BLE scan (loop): BleakError while scanning/connecting: {exc}")
            print(
                "BLE scan: ensure the Raspberry Pi Bluetooth adapter is present, "
                "powered, and not blocked (e.g. check 'rfkill list' and "
                "'systemctl status bluetooth')."
            )
            loop.close()
            return False, None
        loop.close()

    if len(samples) < min_samples:
        print(f"BLE scan: only collected {len(samples)} RSSI samples for {target_name}")
        return False, None

    samples.sort()
    mid = len(samples) // 2
    if len(samples) % 2 == 1:
        median = samples[mid]
    else:
        median = (samples[mid - 1] + samples[mid]) / 2.0

    print(f"BLE scan: samples={samples}, median RSSI={median} dBm")
    return True, median


def scan_ble_advertisement_rssi(bolt_id: str, min_samples: int = 3, timeout_s: float = 30.0):
    """
    Scan for Bolt BLE device advertisements and compute the median RSSI from
    advertisement packets without connecting to the device.
    
    This function collects RSSI values from advertisement packets seen during
    scanning. No GATT connection is made.
    
    Args:
        bolt_id: The Bolt device ID to scan for (e.g., '30000080')
        min_samples: Minimum number of RSSI samples to collect (default: 3)
        timeout_s: Maximum time to spend scanning (default: 30.0 seconds)
    
    Returns:
        Tuple of (ok, median_rssi) where:
        - ok: True if at least min_samples were collected, False otherwise
        - median_rssi: Median RSSI value in dBm, or None if insufficient samples
    """
    target_name = f"Bolt_{bolt_id}"
    try:
        samples = asyncio.run(
            _scan_ble_advertisement_rssi_async(target_name, min_samples, timeout_s)
        )
    except BleakError as exc:
        # Common case on fixtures without a working Bluetooth adapter.
        print(f"BLE scan: BleakError while scanning: {exc}")
        print(
            "BLE scan: ensure the Raspberry Pi Bluetooth adapter is present, "
            "powered, and not blocked (e.g. check 'rfkill list' and "
            "'systemctl status bluetooth')."
        )
        return False, None
    except RuntimeError:
        # If we're already in an event loop, create a dedicated one.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            samples = loop.run_until_complete(
                _scan_ble_advertisement_rssi_async(target_name, min_samples, timeout_s)
            )
        except BleakError as exc:
            print(f"BLE scan (loop): BleakError while scanning: {exc}")
            print(
                "BLE scan: ensure the Raspberry Pi Bluetooth adapter is present, "
                "powered, and not blocked (e.g. check 'rfkill list' and "
                "'systemctl status bluetooth')."
            )
            loop.close()
            return False, None
        loop.close()

    if len(samples) < min_samples:
        print(f"BLE scan: only collected {len(samples)} advertisement RSSI samples for {target_name}")
        return False, None

    samples.sort()
    mid = len(samples) // 2
    if len(samples) % 2 == 1:
        median = samples[mid]
    else:
        median = (samples[mid - 1] + samples[mid]) / 2.0

    print(f"BLE scan: advertisement RSSI samples={samples}, median RSSI={median} dBm")
    return True, median



