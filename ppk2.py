import time
import csv
import os
import re
from datetime import datetime
from typing import Tuple, List
from serial import SerialException  # type: ignore[import-not-found]
from ppk2_api.ppk2_api import PPK2_API  # type: ignore[import-not-found]

ppk2_device = None
device_available = False


def _sort_devices_by_port(devices: List[str]) -> List[str]:
    """
    Sort devices by their ACM port number (e.g., ACM0 before ACM1).
    
    Args:
        devices: List of device paths like ['/dev/ttyACM1', '/dev/ttyACM0']
        
    Returns:
        Sorted list with smallest port number first
    """
    def extract_port_number(device_path: str) -> int:
        """Extract numeric port number from /dev/ttyACM* path."""
        match = re.search(r'ttyACM(\d+)', device_path)
        if match:
            return int(match.group(1))
        # If not ACM format, return a large number to sort to end
        return 999999
    
    return sorted(devices, key=extract_port_number)


devices = PPK2_API.list_devices()
devices = _sort_devices_by_port(devices)
print("PPK2 Devices: " + str(devices))
if (len(devices) == 1 or len(devices) == 2):
    ppk2_device = PPK2_API(devices[0], timeout = 1, write_timeout = 1)
    print("Connected to PPK2 device: " + str(ppk2_device))
    device_available = True
else:
    print("Could not open serial port of PPK2 device.")
    print("Please check USB connections and reset the device.")
    device_available = False

def setup_ppk():
    if device_available:
        ppk2_device.get_modifiers()
        # Configure PPK2 as a source meter and immediately enable DUT power.
        ppk2_device.use_source_meter()
        ppk2_device.set_source_voltage(3300)
        ppk2_device.toggle_DUT_power("ON")
        # PPK2 only actively sources/updates while measuring, so start
        # measuring to mimic the behaviour of the nRF Connect Power Profiler.
        try:
            ppk2_device.start_measuring()
        except Exception as exc:
            print(f"PPK2: failed to start measuring (non-fatal): {exc}")
        return 1
    return 0

def release_ppk():
    if device_available:
        ppk2_device.toggle_DUT_power("OFF")
        try:
            ppk2_device.stop_measuring()
        except Exception as exc:
            print(f"PPK2: failed to stop measuring (non-fatal): {exc}")
        return 1
    return 0

def reconnect_to_ppk():
    devices = PPK2_API.list_devices()
    devices = _sort_devices_by_port(devices)
    print("PPK2 Devices: " + str(devices))
    if (len(devices) == 1):
        ppk2_device = PPK2_API(devices[0], timeout = 1, write_timeout = 1)
        ppk2_device.get_modifiers()
        ppk2_device.use_source_meter()
        ppk2_device.set_source_voltage(3300)
        ppk2_device.toggle_DUT_power("OFF")
        print("Reconnected to PPK2 device: " + str(ppk2_device))
        return True
    else:

        return False


def restart_ppk2() -> bool:
    """
    Fully tear down the current PPK2 handle, wait 1 s, then re-discover and
    re-initialise the device. Used to recover from abnormal sleep-current
    readings after the operator has unplugged/re-plugged the PPK2.

    Returns True if the device is back up and measuring, False otherwise.
    """
    global ppk2_device, device_available

    # Best-effort shutdown of the existing handle. The PPK2 may already be
    # unplugged, in which case these calls raise and we just press on.
    if ppk2_device is not None:
        for action in (
            lambda: ppk2_device.stop_measuring(),
            lambda: ppk2_device.toggle_DUT_power("OFF"),
            lambda: ppk2_device.ser.close(),
        ):
            try:
                action()
            except Exception as exc:
                print(f"PPK2 restart: ignoring teardown error: {exc}")

    ppk2_device = None
    device_available = False

    time.sleep(1.0)

    new_devices = PPK2_API.list_devices()
    new_devices = _sort_devices_by_port(new_devices)
    print("PPK2 Devices (after restart): " + str(new_devices))
    if len(new_devices) not in (1, 2):
        print("PPK2 restart: no device found after re-discovery")
        return False

    try:
        ppk2_device = PPK2_API(new_devices[0], timeout=1, write_timeout=1)
        ppk2_device.get_modifiers()
        ppk2_device.use_source_meter()
        ppk2_device.set_source_voltage(3300)
        ppk2_device.toggle_DUT_power("ON")
        ppk2_device.start_measuring()
    except Exception as exc:
        print(f"PPK2 restart: re-init failed: {exc}")
        ppk2_device = None
        device_available = False
        return False

    device_available = True
    print("PPK2 restart: device re-initialised and measuring")
    return True
    
def device_present() -> bool:
    """
    Return True if at least one PPK2 is currently enumerated on the USB bus.

    Unlike the module-level `device_available` flag (which reflects the last
    successful init), this queries the OS live, so it can distinguish a PPK2
    that has physically dropped off the bus (e.g. after repeated EIO errors)
    from one that is merely in a bad software state.
    """
    try:
        return len(PPK2_API.list_devices()) >= 1
    except Exception as exc:
        print(f"PPK2: device_present() check failed: {exc}")
        return False


def toggle_DUT_power_ON():
    if ppk2_device is None:
        print("PPK2: cannot turn DUT power ON - device handle is None")
        return
    ppk2_device.toggle_DUT_power("ON")

    return

def toggle_DUT_power_OFF():
    if ppk2_device is None:
        print("PPK2: cannot turn DUT power OFF - device handle is None")
        return
    ppk2_device.toggle_DUT_power("OFF")

def set_to_ampere_mode():
    if device_available and ppk2_device is not None:
        # Use source meter mode like Flex, but keep function name for compatibility
        ppk2_device.use_source_meter()
        ppk2_device.set_source_voltage(3300)
        ppk2_device.toggle_DUT_power("ON")

def set_to_source_mode():
    if ppk2_device is None:
        print("PPK2: cannot set source mode - device handle is None")
        return
    ppk2_device.use_source_meter()
    ppk2_device.set_source_voltage(3300)
    # Keep DUT power enabled and measuring when switching back to source mode.
    ppk2_device.toggle_DUT_power("ON")
    try:
        ppk2_device.start_measuring()
    except Exception as exc:
        print(f"PPK2: failed to start measuring in source mode: {exc}")
    
def get_average_current(num_samples):
    if not device_available:
        return -1
    ppk2_device.start_measuring()
    total = 0
    count = 0
    for _ in range(0, num_samples):
        try:
            read_data = ppk2_device.get_data()
            if read_data != b'':
                samples, raw_digital = ppk2_device.get_samples(read_data)
                if len(samples) == 2:
                    # samples is a tuple of (current_samples, voltage_samples)
                    average = sum(samples[0]) / len(samples[0])
                    total = total + average
                    count += 1
                else:
                    # samples is a single array
                    average = sum(samples) / len(samples)
                    total = total + average
                    count += 1
        except Exception as e:
            print(f"PPK2 failed to test current: {e}")
            ppk2_device.stop_measuring()
            return -1
        time.sleep(0.001)  # lower time between sampling -> less samples read in one sampling period

    ppk2_device.stop_measuring()
    if count == 0:
        return -1
    return total / count


def measure_average_current(duration_s: float):
    """
    Measure average current over approximately duration_s seconds.

    Returns current in microamps, or -1 on error.
    """
    if not device_available:
        return -1

    # Use source meter mode like Flex
    ppk2_device.use_source_meter()
    ppk2_device.set_source_voltage(3300)

    ppk2_device.start_measuring()
    total = 0.0
    count = 0
    start = time.time()

    try:
        while time.time() - start < duration_s:
            try:
                read_data = ppk2_device.get_data()
                if read_data != b'':
                    samples, raw_digital = ppk2_device.get_samples(read_data)
                    if len(samples) == 2:
                        average = sum(samples[0]) / len(samples[0])
                    else:
                        average = sum(samples) / len(samples)
                    total += average
                    count += 1
            except Exception:
                print("PPK2 failed to measure current. Please reboot the App and test the DUT again.")
                return -1
            time.sleep(0.001)
    finally:
        try:
            ppk2_device.stop_measuring()
        except Exception:
            pass

    if count == 0:
        return -1
    return total / count


def measure_current_with_csv_report(duration_s: float, csv_filepath: str) -> Tuple[float, bool]:
    """
    Measure current over approximately duration_s seconds and generate a CSV report.

    The CSV file will contain timestamp and current measurements for each sample.

    Args:
        duration_s: Duration to measure in seconds
        csv_filepath: Path where the CSV report should be saved

    Returns:
        Tuple of (average_current_ua, success) where success is True if measurement succeeded
    """
    if not device_available:
        return -1, False

    # Use source meter mode like Flex
    ppk2_device.use_source_meter()
    ppk2_device.set_source_voltage(3300)

    ppk2_device.start_measuring()
    total = 0.0
    count = 0
    start = time.time()
    measurements = []  # List of (timestamp, current_ua) tuples

    try:
        while time.time() - start < duration_s:
            try:
                read_data = ppk2_device.get_data()
                if read_data != b'':
                    samples, raw_digital = ppk2_device.get_samples(read_data)
                    timestamp = time.time() - start  # Relative time in seconds
                    
                    if len(samples) == 2:
                        # Handle case where samples is a tuple of two arrays
                        average = sum(samples[0]) / len(samples[0])
                    else:
                        average = sum(samples) / len(samples)
                    
                    # Samples from PPK2 are already in microamps (uA)
                    current_ua = average
                    measurements.append((timestamp, current_ua))
                    total += average
                    count += 1
            except Exception:
                print("PPK2 failed to measure current. Please reboot the App and test the DUT again.")
                return -1, False
            time.sleep(0.01)
    finally:
        try:
            ppk2_device.stop_measuring()
        except Exception:
            pass

    if count == 0:
        return -1, False

    avg_current_ua = total / count  # Samples are already in microamps

    # Generate CSV report
    try:
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(csv_filepath), exist_ok=True)
        
        with open(csv_filepath, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            # Write header
            writer.writerow(['Timestamp (s)', 'Current (uA)'])
            # Write measurements
            for timestamp, current_ua in measurements:
                writer.writerow([f"{timestamp:.3f}", f"{current_ua:.2f}"])
        
        print(f"Sleep current: CSV report saved to {csv_filepath}")
        print(f"Sleep current: {len(measurements)} samples recorded")
        return avg_current_ua, True
    except Exception as exc:
        print(f"Sleep current: failed to write CSV report: {exc}")
        return avg_current_ua, True  # Still return the average even if CSV write failed

# Main loop used for testing device and script functionality
if __name__ == "__main__":
    print("Setting PPK2 to source meter mode (like Flex).")

    # setup_ppk()
    set_to_source_mode()
    ppk2_device.toggle_DUT_power("ON")

    while True:
        current = get_average_current(100)
        print(f"Current: {current:.2f} uA")
        time.sleep(0.5)
        pass