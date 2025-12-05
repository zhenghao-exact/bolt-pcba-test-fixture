import time
from enum import Enum
from typing import Literal

try:
    import RPi.GPIO as GPIO  # type: ignore[import-not-found]
except Exception as _gpio_exc:  # pragma: no cover - import-time diagnostics only
    GPIO = None  # type: ignore[assignment]
    _GPIO_IMPORT_ERROR = _gpio_exc
else:
    _GPIO_IMPORT_ERROR = None


class CalState(str, Enum):
    """
    Logical states for the Flex Calibrator PCBA.

    These map directly onto the XS3A4051 probe switch channels, driven by three
    Raspberry Pi GPIO lines wired to P_0, P_1 and P_2 on the calibrator board.

    The state numbers used here follow the original firmware / Jira spec:
      - 0 Ohm  -> index 0
      - 27 k   -> index 2
      - 10 k   -> index 3
      - 4.99 k -> index 4
      - 2.2 k  -> index 5
      - 270 k  -> index 6

    The index is then encoded as a 3‑bit value on (P_2 P_1 P_0).
    """

    OFFSET = "offset"  # 0 Ohm (index 0)
    HIGH = "high"  # 270 kOhm (index 6)
    R27K = "27k"  # 0.19 °C reference (27 k, index 2)
    R10K = "10k"  # 25.0 °C reference (10 k, index 3)
    R4K99 = "4k99"  # 44.57 °C reference (4.99 k, index 4)
    R2K2 = "2k2"  # 70.42 °C reference (2.2 k, index 5)


# Raspberry Pi BCM GPIO numbers wired to P_0, P_1, P_2 on the calibrator.
GPIO_P0 = 22  # header pin 11
GPIO_P1 = 17  # header pin 13
GPIO_P2 = 27  # header pin 15
_GPIO_PINS = (GPIO_P0, GPIO_P1, GPIO_P2)

_gpio_initialised = False


def _ensure_gpio() -> bool:
    """
    Initialise Raspberry Pi GPIO for driving the probe switch.

    This configures GPIO17/27/22 (BCM numbering) as push‑pull outputs and drives
    them low by default.
    """
    global _gpio_initialised

    if GPIO is None:
        print(f"[calibrator] RPi.GPIO not available: {_GPIO_IMPORT_ERROR}")
        return False

    if _gpio_initialised:
        return True

    try:
        # Avoid "channel already in use" noise when re-running scripts.
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        for pin in _GPIO_PINS:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
        _gpio_initialised = True
        print(
            f"[calibrator] GPIO initialised (P0={GPIO_P0}, P1={GPIO_P1}, P2={GPIO_P2})"
        )
        return True
    except Exception as exc:  # pragma: no cover - hardware specific failure path
        print(f"[calibrator] Failed to initialise GPIO: {exc}")
        return False


# Mapping from logical calibrator state to the 3‑bit channel index used by the
# XS3A4051 switch.
#
# Empirically, with your wiring:
#   index 0 -> 0 Ω (OFFSET)
#   index 1 -> 27 k equivalent (0.19 °C point)
#   index 2 -> 10 k equivalent (25 °C point)
#   index 3 -> 4.99 k equivalent (44.57 °C point)
#   index 4 -> 2.2 k equivalent (70.42 °C point)
#   index 5 -> 270 k equivalent (HIGH calibration point)
#   index 6/7 -> unused
STATE_TO_INDEX: dict[CalState, int] = {
    CalState.OFFSET: 0,
    CalState.R27K: 1,
    CalState.R10K: 2,
    CalState.R4K99: 3,
    CalState.R2K2: 4,
    CalState.HIGH: 5,
}


def _coerce_state(state: CalState | str) -> CalState:
    if isinstance(state, CalState):
        return state
    return CalState(state)


def _drive_index(index: int) -> None:
    """
    Drive P_0, P_1, P_2 according to the given 3‑bit index.

    Bit mapping (binary index = P_2 P_1 P_0):
      P_0 = bit 0 (LSB)
      P_1 = bit 1
      P_2 = bit 2 (MSB)
    """
    b0 = index & 0x1
    b1 = (index >> 1) & 0x1
    b2 = (index >> 2) & 0x1

    GPIO.output(GPIO_P0, b0)
    GPIO.output(GPIO_P1, b1)
    GPIO.output(GPIO_P2, b2)


def set_state(
    state: CalState | Literal["offset", "high", "27k", "10k", "4k99", "2k2"],
    settle_s: float = 0.05,
) -> bool:
    """
    Select a calibration state on the Flex Calibrator PCBA.

    This uses three Raspberry Pi GPIOs wired directly to the probe switch
    select lines instead of the original TMP1826 / 1‑Wire path.
    """
    try:
        cal_state = _coerce_state(state)
    except ValueError as exc:
        print(f"[calibrator] Invalid state '{state}': {exc}")
        return False

    if not _ensure_gpio():
        print(f"[calibrator] Cannot set state {state}: GPIO not ready")
        return False

    index = STATE_TO_INDEX.get(cal_state)
    if index is None:
        print(f"[calibrator] No index mapping for state {cal_state}")
        return False

    try:
        _drive_index(index)
    except Exception as exc:  # pragma: no cover - hardware specific failure path
        print(f"[calibrator] Failed to drive GPIOs for state {cal_state}: {exc}")
        return False

    print(
        f"[calibrator] set_state -> {cal_state} (index={index}, "
        f"P0={index & 1}, P1={(index >> 1) & 1}, P2={(index >> 2) & 1})"
    )

    if settle_s > 0:
        time.sleep(settle_s)
    return True



