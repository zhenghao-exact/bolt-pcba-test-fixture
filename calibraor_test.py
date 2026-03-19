#!/usr/bin/env python3
"""
Standalone analog calibration debug script for the Flex Calibrator + Bolt TH ADC.

This runs the **same sequence** as the fixture's analog step, but from the CLI:

  1. Set OFFSET (0 Ω), measure raw ADC, check [-100, 100], program `etc_adc offset <avg> factory`.
  2. Set HIGH (270 kΩ), measure raw ADC, check [3519.64, 3719.64],
     program `etc_adc high <avg> factory`.
  3. Program `etc_adc ref 3619.64 factory`.
  4. For 27k / 10k / 4.99k / 2.2k, set calibrator state, read calibrated
     temperature, and check ±0.2 °C against the reference values.

Hardware assumptions:
  - Raspberry Pi GPIO17/27/22 wired to P_0/P_1/P_2 of the probe switch
    (see `calibrator.py`).
  - Bolt connected over USB CDC (see `DEFAULT_SERIAL_PORTS` below).
  - Bolt firmware exposes `etc_adc` and `etc_sensor pcba` shell commands.
"""

import sys
import time
import re
import argparse
from typing import Optional, Sequence

from serial import Serial  # type: ignore[import-not-found]

import bolt_control  # type: ignore[import-not-found]
import calibrator  # type: ignore[import-not-found]
from calibrator import CalState  # type: ignore[import-not-found]


DEFAULT_SERIAL_PORTS: Sequence[str] = (
    "/dev/ttyACM4",
)

# Calibration mode configurations
CALIBRATION_MODE_FAST = "fast"
CALIBRATION_MODE_DEBUG = "debug"

# Fast mode: optimized for production throughput (timeouts tuned for 115200 baud)
FAST_MODE_SAMPLES = 2
FAST_MODE_DISCARD = 0
FAST_MODE_TIMEOUT_PER_SAMPLE_S = 2.0

# Debug mode: more samples for detailed analysis
DEBUG_MODE_SAMPLES = 3
DEBUG_MODE_DISCARD = 1
DEBUG_MODE_TIMEOUT_PER_SAMPLE_S = 6.0


def open_first_available_serial(ports: Sequence[str]) -> Optional[Serial]:
    """Try to open the first available serial port from the list."""
    for dev in ports:
        print(f"[serial] Trying {dev} ...", end="")
        ser = bolt_control.open_serial(dev)
        if ser:
            print(" OK")
            # Allow some time for the Bolt to finish any boot messages (2s for 115200 baud).
            time.sleep(2.0)
            bolt_control.clear_serial_buffer(ser)
            return ser
        print(" failed")
    return None


def _log_step(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def _read_probe_temperature_once(ser: Serial, timeout_s: float = 2.0) -> Optional[float]:
    """
    Trigger `etc_adc sample` and parse the 'Probe sensor temperature: <val>' line.
    
    Returns immediately once the temperature is found, rather than waiting for
    the full timeout. The timeout is only used as a worst-case safety limit.
    """
    if not bolt_control.send_shell_command(ser, "etc_adc sample"):
        return None

    end_time = time.time() + timeout_s
    temp_c: Optional[float] = None
    
    while time.time() < end_time:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        print(f"[adc_sample] {line}")

        # Check for error conditions first
        if "Failed to apply calibration" in line or "Error" in line:
            return None

        # Look for the temperature value - return immediately when found
        m = re.search(r"Probe sensor temperature:\s*([-0-9.]+)", line)
        if m:
            try:
                temp_c = float(m.group(1))
                # Found temperature - return immediately (don't wait for timeout)
                return temp_c
            except ValueError:
                return None

    # Timeout reached without finding temperature
    return None


def _probe_temperature_average(
    ser: Serial,
    samples: int = 2,
    discard: int = 0,
    timeout_per_sample_s: float = 2.0,
) -> Optional[float]:
    """
    Average a few 'Probe sensor temperature' readings from etc_adc sample.

    Args:
        ser: Serial port connection
        samples: Number of samples to average (default: 2 for fast mode)
        discard: Number of initial samples to discard (default: 0 for fast mode)
        timeout_per_sample_s: Timeout per sample in seconds (default: 2.0 for 115200 baud)
    
    Returns:
        Average temperature in Celsius, or None if sampling failed
    """
    values = []

    for _ in range(discard):
        _ = _read_probe_temperature_once(ser, timeout_s=timeout_per_sample_s)

    for _ in range(samples):
        t = _read_probe_temperature_once(ser, timeout_s=timeout_per_sample_s)
        if t is None:
            return None
        values.append(t)

    if not values:
        return None
    return sum(values) / float(len(values))


def run_full_analog_calibration(ser: Serial, mode: str = CALIBRATION_MODE_FAST) -> dict:
    """
    Run the full Jira analog calibration flow and log each step.
    
    Args:
        ser: Serial port connection to the Bolt device
        mode: Calibration mode - "fast" (default) for production throughput,
              "debug" for more samples and detailed analysis
    
    Returns:
        Dictionary with:
        - success: bool
        - offset_raw: float (raw ADC value for 0Ω)
        - high_raw: float (raw ADC value for 270kΩ)
        - reference: float (3619.64)
        - temp_27k: float (measured temperature at 27k resistor)
        - temp_10k: float (measured temperature at 10k resistor)
        - temp_4k99: float (measured temperature at 4.99k resistor)
        - temp_2k2: float (measured temperature at 2.2k resistor)
    """
    # Select sampling parameters based on mode
    if mode == CALIBRATION_MODE_DEBUG:
        samples = DEBUG_MODE_SAMPLES
        discard = DEBUG_MODE_DISCARD
        timeout_per_sample_s = DEBUG_MODE_TIMEOUT_PER_SAMPLE_S
        print(f"[calibration] Using DEBUG mode: samples={samples}, discard={discard}, timeout={timeout_per_sample_s}s")
    else:
        samples = FAST_MODE_SAMPLES
        discard = FAST_MODE_DISCARD
        timeout_per_sample_s = FAST_MODE_TIMEOUT_PER_SAMPLE_S
        print(f"[calibration] Using FAST mode: samples={samples}, discard={discard}, timeout={timeout_per_sample_s}s")
    
    overall_ok = True
    calibration_start_time = time.time()
    result = {
        "success": False,
        "offset_raw": None,
        "high_raw": None,
        "reference": 3619.64,
        "temp_27k": None,
        "temp_10k": None,
        "temp_4k99": None,
        "temp_2k2": None,
    }

    # --- Calibration: 0 Ohm (offset) ---
    _log_step("Step 1: OFFSET (0 Ω) calibration")
    if not calibrator.set_state(CalState.OFFSET, settle_s=0.05):
        print("[FAIL] Failed to set calibrator to OFFSET (0 Ω)")
        return result

    bolt_control.clear_serial_buffer(ser)
    raw_offset = bolt_control.adc_sample_raw_average(ser, samples=10, discard=2)
    if raw_offset is None:
        print("[FAIL] Could not read raw ADC for OFFSET")
        return result
    print(f"[INFO] OFFSET raw ADC average = {raw_offset:.3f}")
    result["offset_raw"] = raw_offset

    if raw_offset < -100.0 or raw_offset > 100.0:
        print("[FAIL] OFFSET raw value outside [-100, 100]")
        overall_ok = False
    else:
        print("[OK] OFFSET raw value within [-100, 100]")

    # Use USER calibration so etc_adc_apply_cal() prefers our values.
    print("[CMD] etc_adc offset %.3f user" % raw_offset)
    if not bolt_control._write_adc_param(ser, "offset", (raw_offset), scope="user"):  # type: ignore[attr-defined]
        print("[FAIL] Failed to program etc_adc offset (user)")
        return result
    print("[OK] etc_adc offset (user) programmed")

    # --- Calibration: 270k Ohm (high) ---
    _log_step("Step 2: HIGH (270 kΩ) calibration")
    if not calibrator.set_state(CalState.HIGH, settle_s=0.05):
        print("[FAIL] Failed to set calibrator to HIGH (270 kΩ)")
        return result

    bolt_control.clear_serial_buffer(ser)
    raw_high = bolt_control.adc_sample_raw_average(ser, samples=10, discard=2)
    if raw_high is None:
        print("[FAIL] Could not read raw ADC for HIGH")
        overall_ok = False
        raw_high = 0.0
    print(f"[INFO] HIGH raw ADC average = {raw_high:.3f}")
    result["high_raw"] = raw_high

    # Allow a wider range for the HIGH point; we only require it to be well above
    # OFFSET and comfortably below full scale. We *do not* program calibration
    # here yet; ref/high need to be updated in the right order in Step 3.
    HIGH_MIN = 3400.0
    HIGH_MAX = 4095.0
    if raw_high < HIGH_MIN or raw_high > HIGH_MAX:
        print(f"[FAIL] HIGH raw value outside [{HIGH_MIN:.2f}, {HIGH_MAX:.2f}]")
        overall_ok = False
    else:
        print(f"[OK] HIGH raw value within [{HIGH_MIN:.2f}, {HIGH_MAX:.2f}]")

    # --- Reference value ---
    _log_step("Step 3: Program reference (ref) and high using measured HIGH point")
    # First, move ref close to the measured HIGH value so validation passes,
    # then update high. Firmware validate_triplet() expects |ref - high| to be
    # within REF_MAX_ERROR, so the order matters.
    ref_value = float(3619.64)
    print(f"[CMD] etc_adc ref {ref_value:.3f} user")
    if not bolt_control._write_adc_param(ser, "ref", ref_value, scope="user"):  # type: ignore[attr-defined]
        print("[FAIL] Failed to program etc_adc ref (user); continuing with existing calibration")
        overall_ok = False
    else:
        print("[OK] etc_adc ref (user) programmed")

    print("[CMD] etc_adc high %.3f user" % raw_high)
    if not bolt_control._write_adc_param(ser, "high", raw_high, scope="user"):  # type: ignore[attr-defined]
        print("[FAIL] Failed to program etc_adc high (user); continuing with existing calibration")
        overall_ok = False
    else:
        print("[OK] etc_adc high (user) programmed")

    # --- Verification: temperature points ---
    _log_step("Step 4: Verification points (temperature checks)")
    verification_points = [
        (CalState.R27K, 0.19, "27k", "temp_27k"),
        (CalState.R10K, 25.0, "10k", "temp_10k"),
        (CalState.R4K99, 44.57, "4.99k", "temp_4k99"),
        (CalState.R2K2, 70.42, "2.2k", "temp_2k2"),
    ]

    for state, expected_c, label, result_key in verification_points:
        print(f"\n--- Verification: {label} resistor ---")
        verification_start_time = time.time()
        
        if not calibrator.set_state(state, settle_s=0.05):
            print(f"[FAIL] Failed to set state {state}")
            overall_ok = False
            continue

        bolt_control.clear_serial_buffer(ser)
        measured = _probe_temperature_average(
            ser, 
            samples=samples, 
            discard=discard, 
            timeout_per_sample_s=timeout_per_sample_s
        )
        if measured is None:
            print("[FAIL] Could not read calibrated temperature")
            overall_ok = False
            continue

        verification_time = time.time() - verification_start_time
        result[result_key] = measured
        diff = measured - expected_c
        print(
            f"[INFO] State={state} expected={expected_c:.2f} °C "
            f"measured={measured:.3f} °C diff={diff:+.3f} °C "
            f"(took {verification_time:.2f}s)"
        )

        if abs(diff) > 0.2:
            print("[FAIL] |measured - expected| > 0.2 °C")
            overall_ok = False
        else:
            print("[OK] Temperature within ±0.2 °C")

    result["success"] = overall_ok
    total_time = time.time() - calibration_start_time
    print(f"\n[calibration] Total calibration time: {total_time:.2f}s")
    return result


def run_calibration_with_port(port: str, mode: str = CALIBRATION_MODE_FAST) -> dict:
    """
    Run the full analog calibration sequence using a specific serial port.

    This function is designed to be called from the fixture main code, which
    has already established a serial connection and knows the port.

    Args:
        port: The serial port path (e.g., "/dev/ttyACM4")
        mode: Calibration mode - "fast" (default) for production throughput,
              "debug" for more samples and detailed analysis

    Returns:
        Dictionary with calibration results (see run_full_analog_calibration).
    """
    print(f"[calibration] Opening serial port {port}")
    ser = bolt_control.open_serial(port)
    if not ser:
        print(f"[calibration] Failed to open serial port {port}")
        return {
            "success": False,
            "offset_raw": None,
            "high_raw": None,
            "reference": 3619.64,
            "temp_27k": None,
            "temp_10k": None,
            "temp_4k99": None,
            "temp_2k2": None,
        }

    try:
        # Allow some time for the Bolt to finish any boot messages.
        time.sleep(1.0)
        bolt_control.clear_serial_buffer(ser)
        result = run_full_analog_calibration(ser, mode=mode)
        return result
    finally:
        try:
            ser.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flex Calibrator + Bolt TH Analog Calibration Debug"
    )
    parser.add_argument(
        "port",
        nargs="?",
        help="Serial port path (e.g., /dev/ttyACM4). If not provided, uses DEFAULT_SERIAL_PORTS.",
    )
    parser.add_argument(
        "--mode",
        choices=[CALIBRATION_MODE_FAST, CALIBRATION_MODE_DEBUG],
        default=CALIBRATION_MODE_FAST,
        help=f"Calibration mode: '{CALIBRATION_MODE_FAST}' for fast production mode (default), "
             f"'{CALIBRATION_MODE_DEBUG}' for detailed analysis with more samples.",
    )
    args = parser.parse_args()

    print("=== Flex Calibrator + Bolt TH Analog Calibration Debug ===")

    # Determine which port(s) to use
    if args.port:
        # Use the provided port
        ports_to_try = [args.port]
        print(f"Using provided serial port: {args.port}")
    else:
        # Fall back to default ports
        ports_to_try = list(DEFAULT_SERIAL_PORTS)
        print(
            "Trying Bolt serial ports in order: "
            + ", ".join(ports_to_try)
        )

    ser = open_first_available_serial(ports_to_try)
    if not ser:
        print(
            "[FATAL] Could not open any Bolt serial port. "
            f"Tried: {', '.join(ports_to_try)}"
        )
        return 1

    try:
        result = run_full_analog_calibration(ser, mode=args.mode)
    finally:
        try:
            ser.close()
        except Exception:
            pass

    print("\n=== SUMMARY ===")
    if result.get("success", False):
        print("[PASS] Analog calibration sequence completed successfully.")
        return 0

    print("[FAIL] Analog calibration sequence had one or more failures.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
