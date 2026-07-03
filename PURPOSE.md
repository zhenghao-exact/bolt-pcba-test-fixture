# Purpose

Raspberry-Pi host scripts for the **Bolt PCBA production test fixture** — provisions and
tests Bolt PCBAs during manufacturing.

Per-board flow: flash **test firmware** → run shell-driven subsystem checks over the Zephyr
shell → flash **production firmware** (full `merged.hex`, bootloader included) → sleep-current
test → print label + upload results.

## Board scope (post FW-1049 / FW-1054)

- **Every board through this fixture is a reworked ("remake") 1.3.0 board.** The original
  1.3.0 board is retired from this line — we no longer assume non-remake boards exist here.
- **Production firmware is the `bolt-remake` build, signed with the custom MCUboot key**
  (FW-1049). The production flash writes the whole `merged.hex`, *including the MCUboot
  bootloader*, so **this fixture is where the custom-key bootloader is baked into each board** —
  the cutover that lets a board accept the key-matched OTA images the gateway serves
  (FW-1049 phase 2). Staging a default-key / non-remake hex here silently ships a board that
  **cannot OTA**.
- **Test firmware is the `bolt-poc-remake` build** — the PoC overlay enables the Zephyr shell
  the fixture drives, and it carries the remake HW config. Its signing key is irrelevant (the
  production flash overwrites it); only the production image's key matters on the shipped board.

## Strain-gauge (SG) boards are separate

SG boards do **not** run the normal fixture flow. SG is a separate flash-and-verify step
(confirm the board works). SG firmware comes from the separate SG pipeline (FW-909 branch,
default key, no remake — there is deliberately no `bolt-sg-remake`). In this repo SG is reached
via the `--SG` flag, which skips the IMU / analog-cal / sleep-current steps.

## Entry points

- `bolt_fixture_main.py` — the GUI production fixture (operator-facing, the main runner).
- `main_non_gui.py` — headless **mirror of the above, used for manual testing**: same production
  logic (BLE retry, PPK2 escalation, `--SG`/`--SKIP_CAL`, label + upload), terminal-driven, with
  a per-step run/skip gate.

Keep the two in sync — a change to the flashing/test flow in one should be mirrored in the other.

## Firmware staging

Place the current CI builds on the Pi under `FW_FOLDER_PATH`:

| Fixture file | CI artifact | Notes |
|---|---|---|
| `bolt-remake-prod-fw.hex` | `bolt-remake_<ver>.hex` | production, **custom key** |
| `bolt-remake-test-fw.hex` | `bolt-poc-remake_<ver>.hex` | test, PoC shell |
| `bolt-sg-prod-fw.hex` | SG pipeline build | separate, default key |

## Signing-key guard

Before every non-SG production flash, the fixture verifies the **staged production `.hex` is
signed with the custom key**: `fw_keyhash.py` extracts the MCUboot `IMAGE_TLV_KEYHASH` from the
merged hex and the flash is **aborted** unless it equals the custom key
(`348092B8…`; the default/insecure key is `FC5701DC…`). Combined with `nrfjprog`'s `VERIFY_HASH`
on the flash itself, this guarantees every shipped board runs the custom-key bootloader — a
mis-staged default-key hex fails the fixture instead of shipping an un-OTA-able board.

This is a file-level check (deterministic, no BLE) rather than a post-flash BLE read: the
pubkey characteristic (`4a7b9d13`) is encrypted and the fixture's BLE test is advertisement-only
(no GATT connect/pair), and since `nrfjprog` verifies the flash against the file, checking the
file's key is equivalent to reading the running image. SG production is a separate default-key
flow and is not key-checked.
