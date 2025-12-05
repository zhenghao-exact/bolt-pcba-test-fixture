import time
import logging
from pynrfjprog import HighLevel, APIError

# Returns False if no device is registered, True on successful flash
def flash_FW(fw_path):
    with HighLevel.API() as api:
        snrs = api.get_connected_probes()
        if len(snrs) == 0:
            return False
        try:
            with HighLevel.DebugProbe(api, snrs[0]) as probe:
                probe.program(fw_path, HighLevel.ProgramOptions(verify = HighLevel.VerifyAction.VERIFY_HASH, erase_action=HighLevel.EraseAction.ERASE_SECTOR, reset=HighLevel.ResetAction.RESET_HARD))
                return True
        except:
            print("Unable to flash DUT. Reseat the device inside of the fixture and try again.\n")
            print("If this problem persists, shutdown the Raspberry Pi and power cycle the Bolt Fixture.\n")
            return False
            
        
# Main loop used for testing device and script functionality
if __name__  == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger()
    flash_FW(0)
    # time.sleep(2)
    # flash_FW(0)
