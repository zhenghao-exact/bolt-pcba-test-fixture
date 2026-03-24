import datetime
import csv
import os

date = datetime.date.today()

year = date.year
month = date.month

# Write results in CSV by fiscal year
if date.month > 6:
    year = year + 1

test_result_filepath = f"/home/boltfixturepi/Documents/bolt-pcba-test-fixture/{year}.csv"

test_result_header = [
    "Fixture ID", "Date", "Time (Test ID)", "User", "Work Order", "PCBA ID", 
    "Device ID", "HW ID", "IMU Test", "BLE Test", "BLE RSSI (dBm)", 
    "Analog Calibration", "ADC Offset (raw)", "ADC High (raw)", "ADC Reference", 
    "Temp 0.19 (°C)", "Temp 25 (°C)", "Temp 44.57 (°C)", "Temp 70.42 (°C)",
    "Sleep Current Test", "Sleep Current (uA)", "Supply Voltage (V)", "Final Result"
]


def assemble_row(results: dict, measurements: dict, user: str, fixture: int):
    """
    Assemble a CSV row from test results and measurements.
    
    Args:
        results: Dictionary of test results (PASS/FAIL)
        measurements: Dictionary of measured values
        user: Username (set to 'N/A' for Bolt)
        fixture: Fixture ID (always 1 for Bolt)
    
    Returns:
        List of values for the CSV row
    """
    csv_row = []
    
    # Basic info
    csv_row.append(str(fixture))  # Fixture ID
    csv_row.append(datetime.date.today())  # Date
    csv_row.append(measurements.get("test_ID", ""))  # Time (Test ID)
    csv_row.append(user)  # User
    csv_row.append("N/A")  # Work Order
    csv_row.append(measurements.get("PCBA_ID", "N/A"))  # PCBA ID
    csv_row.append(measurements.get("dev_ID", ""))  # Device ID
    csv_row.append("N/A")  # HW ID
    
    # IMU Test
    csv_row.append(results.get("imu", False))
    
    # BLE Test
    csv_row.append(results.get("ble", False))
    csv_row.append(measurements.get("ble_rssi_median"))  # BLE RSSI (dBm)
    
    # Analog Calibration
    csv_row.append(results.get("analog", False))
    csv_row.append(measurements.get("adc_offset_raw_factory"))  # ADC Offset (raw)
    csv_row.append(measurements.get("adc_high_raw_factory"))  # ADC High (raw)
    csv_row.append(measurements.get("adc_ref_factory", 3619.64))  # ADC Reference
    csv_row.append(measurements.get("adc_temp_27k_measured_c"))  # Temp 27k (°C)
    csv_row.append(measurements.get("adc_temp_10k_measured_c"))  # Temp 10k (°C)
    csv_row.append(measurements.get("adc_temp_4k99_measured_c"))  # Temp 4.99k (°C)
    csv_row.append(measurements.get("adc_temp_2k2_measured_c"))  # Temp 2.2k (°C)
    
    # Sleep Current (column shows "skipped" when operator skipped prod flash + sleep in GUI)
    if measurements.get("sleep_current_skipped") or measurements.get("sleep_current_ua") == "SKIPPED":
        csv_row.append("skipped")
    else:
        csv_row.append(results.get("sleep_current", False))
    csv_row.append(measurements.get("sleep_current_ua"))  # Sleep Current (uA), may be "SKIPPED"
    
    # Supply Voltage (fixed 3.3V)
    csv_row.append(measurements.get("supply_voltage_v", 3.3))
    
    # Final Result
    csv_row.append(results.get("final", False))
    
    return csv_row


def write_test_results(results, measurements, user, fixture):
    """
    Write test results to CSV file.
    
    Args:
        results: Dictionary of test results
        measurements: Dictionary of measured values
        user: Username (set to 'N/A' for Bolt)
        fixture: Fixture ID (always 1 for Bolt)
    """
    row = assemble_row(results, measurements, user, fixture)
    
    # If file does not exist, create it and add headers
    if not os.path.exists(test_result_filepath):
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(test_result_filepath), exist_ok=True)
        with open(test_result_filepath, "x", newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(test_result_header)
            csvfile.close()
    
    # Write test data to next row
    with open(test_result_filepath, 'a', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(row)
        csvfile.close()
