Bolt PCBA Test Fixture
======================

This project contains the Raspberry Pi host‑side scripts used to test and provision
**Bolt** PCBAs during manufacturing.  It is heavily based on the existing
`etc-monitor-fixture` project and reuses many of the same helper modules
(`ppk2`, `nrfjprog`, `printer_manager`, etc.).

At a high level the fixture will:

- Prompt the operator for required inputs (user, work order, Bolt QR / serial).
- Flash Bolt **test firmware** using `nrfjprog`.
- Run a sequence of automated checks against the DUT via the Zephyr shell.
- Flash **production firmware** when all checks pass.
- Print a label and upload results.

The detailed test content is intentionally lightweight for now – most subsystem
tests (BLE, IMU, analog calibration, modem, etc.) are implemented as
placeholders that call into shell commands on the DUT.  These shell commands
will be implemented in the Bolt firmware.

## Layout

- `bolt_fixture_main.py` – entry point that orchestrates the GUI flow and
  executes tests.
- `bolt_control.py` – serial helpers for talking to the Bolt Zephyr shell
  (wrapping the Bolt‑specific shell commands).
- Reused helpers copied from `etc-monitor-fixture`:
  - `ppk2.py`
  - `nrfjprog.py`
  - `printer_manager.py`
  - `csv_manager.py`
  - `upload_results.py`
  - `ADS1115.py`, `PCA9675.py`, `VEML3328.py`, `LoRa.py`
  - `generate_psk.py`
  - `gui.py` (generic fixture GUI)

## Dependencies

Install Python requirements on the Raspberry Pi:

```bash
cd bolt-pcba-test-fixture
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Running

From the project root on the Pi:

```bash
python -m bolt_pcba_fixture.bolt_fixture_main
```

The application will guide the operator through scanning the Bolt QR code and
running the test sequence.


