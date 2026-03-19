from tkinter import *
from tkinter import font
from tkinter import ttk
import time


class App(Tk):
    green  = '#00FF00'
    red    = '#FF0000'
    white  = '#FFFFFF'

    def __init__(self):
        super().__init__()
        self.test_in_progress = False
        self.window_closed = False
        self.dumpster_sample = False
        
        self.pcba_barcode = StringVar()
        self.usb_replug_var = IntVar()
        self.acknowledge_info_var = IntVar()
        self.test_complete_var = IntVar()
        self.pcba_barcode_scan_var = IntVar()
        self.stop_test_var = IntVar()
        self.print_label_var = IntVar()
        self.imu_instruction_var = IntVar()
        self.sleep_current_var = IntVar()
        self.restart_fixture_var = IntVar()
        self.reboot_pi_var = IntVar()
        self.ble_retry_var = IntVar()

        # self.attributes("-fullscreen", True)
        self.bind("<Escape>", self.end_fullscreen)
        
        self.geometry("800x480")
        
        self.title("Bolt PCBA Test Fixture")
    
        self.frame = ttk.Frame(self, padding = 10)
        self.frame.grid()
        
        default_font = font.nametofont("TkFixedFont")
        default_font.config(size=9)
        self.option_add("*Font", "TkFixedFont")

        self.ID_string = StringVar(value = "Bolt PCBA Test Fixture | ID under test: ")
        ttk.Label(self.frame, text="Bolt PCBA Test Fixture").grid(column=0, row=0, columnspan=6)
        
        # Add test features (Bolt-specific 10-step flow)
        ttk.Label(self.frame, text ="Bolt PCBA Test Fixture")
        ttk.Label(self.frame, text = "Scan Bolt QR..............").grid(column=0, row=1, sticky=W)
        ttk.Label(self.frame, text = "Flash Test Firmware.......").grid(column=0, row=2, sticky=W)
        ttk.Label(self.frame, text = "USB Connection Test.......").grid(column=0, row=3, sticky=W)
        ttk.Label(self.frame, text = "Set Serial Number.........").grid(column=0, row=4, sticky=W)
        ttk.Label(self.frame, text = "IMU Test (manual).........").grid(column=0, row=5, sticky=W)
        ttk.Label(self.frame, text = "BLE Test..................").grid(column=0, row=6, sticky=W)
        ttk.Label(self.frame, text = "Analog Calibration........").grid(column=0, row=7, sticky=W)
        ttk.Label(self.frame, text = "Flash Production Firmware.").grid(column=0, row=8, sticky=W)
        ttk.Label(self.frame, text = "Sleep Current Test........").grid(column=0, row=9, sticky=W)
        ttk.Label(self.frame, text = "Final Result..............").grid(column=0, row=10, sticky=W)
        
        # Add indicators (one column, 10 steps)
        for i in range(10):
                self.__create_circle(column=1, row=i+1, color=self.white)

        # Add serial output display
        self.serial_frame = Text(self.frame, width=50, height = 25)
        self.serial_frame.grid(column=4, row=1, rowspan=13, sticky=E)
        self.scrollbar = Scrollbar(self.frame)
        self.scrollbar.grid(column=5, row=1, rowspan=13, sticky=NS)
        self.serial_frame.config(yscrollcommand=self.scrollbar.set)
        self.scrollbar.config(command=self.serial_frame.yview)
                
        self.status_string = StringVar(value = "STATUS: Ready to begin test")
        
        # Status Display
        status_label = ttk.Label(self.frame, textvariable=self.status_string, width=36, anchor=CENTER, wraplength=280, justify=CENTER)
        status_label.grid(column = 0, row = 12, columnspan=4)
        status_label.config(font=("TkFixedFont", 12))
        
        def cancel_button_pressed():
            self.stop_test_var.set(1)
            return
        
        ttk.Button(self.frame, text= "CANCEL", command=cancel_button_pressed).grid(column = 0, row = 14, columnspan=4)

        # ttk.Button(self.frame, text="Upload", command=upload_button_pressed).grid(column = 0, row = 13, columnspan=4)

        # Initialize window
        self.update()
        
    # Updates window
    def update_window(self):
        self.frame.update()
        return
        
    # Used to disable the close window button on pop-up menus
    def disable_event(self):
        pass
        
    def get_pcba_barcode(self):
        return self.pcba_barcode.get()
    
    # Creates a coloured circle in the specified grid location
    def __create_circle(self, column, row, color):
        circle = Canvas(self.frame, width = 16, height = 16)
        circle.create_oval(3, 3, 13, 13, fill=color)
        circle.grid(column = column, row = row, sticky=W)
        return
    
    # Updates display with current test status
    def update_test_display(self, state: str):
        if state == "active":
            self.status_string.set(value="STATUS: Testing in progress...")
        elif state == "complete":
            self.status_string.set(value="STATUS: Test complete.\nReady for next test")
        self.frame.update()
        return
    
    # Set colour indicators to to white at start of test
    def reset_indicators(self):
        for test_number in range(10):
            self.frame.grid_slaves(column=1, row=test_number+1)[0].destroy()
            self.__create_circle(column=1, row=test_number+1, color=self.white)
        self.frame.update()
        return 

    # Called whenever the serial recieved a message
    def update_serial_display(self, serial_buffer):
        self.serial_frame.insert(END, serial_buffer)
        self.serial_frame.see(END)
        return
        
    # Used to bind escape key to ending fullscreen application
    def end_fullscreen(self, event=None):
        self.attributes("-fullscreen", False)
        return "break"
    
    # Updates the indicator next to test depending on pass or fail
    def update_test_indicator(self, test_number, passed):
        if passed: color = self.green
        else: color = self.red
        
        if 1 <= test_number <= 10:
            self.frame.grid_slaves(column=1, row=test_number)[0].destroy()
            self.__create_circle(column=1, row=test_number, color=color)
    
        self.frame.update()
        return
    
    ## POP-UP WINDOWS ##
    
    def no_internet_window(self):
        no_internet_popup = Toplevel(padx=20, pady=20)
        no_internet_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        information_string = "The device is not currently connected to the internet.\n\n" + \
                             "Please check connection and try again. The application will shut off now"
                        
        def acknowledge_info():
            self.acknowledge_no_internet_var.set(1)
            no_internet_popup.destroy()
            self.destroy()
            return
        
        no_internet_popup.title("No connection")
        
        information_label = ttk.Label(no_internet_popup, text=information_string, anchor=W)
        information_label.config(font=("TkFixedFont", 14))
        acknowledge_button = ttk.Button(no_internet_popup, text="OK", command=acknowledge_info)
        
        information_label.grid(column=0, row=0, columnspan=1)
        acknowledge_button.grid(column=0, row=1, padx=10, pady=10)
        
        return no_internet_popup
    
    # Initial pop-up window when opening the app for the first time.
    def information_window(self):
        information_popup = Toplevel(padx=20, pady=20)
        information_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        information_string = (
            "BOLT PCBA FIXTURE SETUP:\n\n"
            "1. Ensure the lid is open and no device is on the test bed.\n\n"
            "2. Plug in devices in this order:\n"
            "   a) Nordic PPK2 (will appear as /dev/ttyACM0).\n"
            "   b) nRF flashing/debug board.\n"
            "   c) Bolt PCBA USB.\n"
            "   d) Scanner and label printer.\n\n"
            "3. Power on the label printer and ensure it is ready.\n\n"
            "4. Connect the PPK2 alligator clips and custom Bolt as per the "
            "fixture diagram.\n"
        )
        
        ## 1 == OK button selected
        def acknowledge_info():
            self.acknowledge_info_var.set(1)
            information_popup.destroy()
            return
               
        information_popup.title("IMPORTANT IMFORMATION")
        
        information_label = ttk.Label(information_popup, text=information_string, anchor=W)
        information_label.config(font=("TkFixedFont", 14))
        acknowledge_button = ttk.Button(information_popup, text="OK", command=acknowledge_info)
        
        information_label.grid(column=0, row=0, columnspan=1)
        acknowledge_button.grid(column=0, row=1, padx=10, pady=10)
        
        return information_popup
    
    # IMU instruction popup for manual rotation step
    def imu_instruction_window(self):
        imu_popup = Toplevel(padx=20, pady=20)
        imu_popup.protocol("WM_DELETE_WINDOW", self.disable_event)

        def acknowledge():
            self.imu_instruction_var.set(1)
            imu_popup.destroy()
            return

        imu_popup.title("IMU Test Instructions")

        msg = (
            "IMU TEST:\n\n"
            "Press OK to begin the IMU test.\n\n"
            "With in one minute, rotate the Bolt PCBA at least 45° in each direction as shown in \n\n"
            "the work instructions.\n\n"
            "Move it slowly to ensure the IMU can detect the angle change.\n\n"
        )

        label = ttk.Label(imu_popup, text=msg, anchor=W, justify=LEFT)
        label.config(font=("TkFixedFont", 14))
        ok_button = ttk.Button(imu_popup, text="OK", command=acknowledge)

        label.grid(column=0, row=0, padx=10, pady=10)
        ok_button.grid(column=0, row=1, padx=10, pady=10)

        # Wait until the user has acknowledged the instructions
        self.imu_instruction_var.set(0)
        self.wait_variable(self.imu_instruction_var)

    # Sleep current test instructions
    def sleep_current_window(self):
        popup = Toplevel(padx=20, pady=20)
        popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        def acknowledge():
            self.sleep_current_var.set(1)
            popup.destroy()
            return
    
        popup.title("Sleep Current Test Instructions")

        msg = (
            "SLEEP CURRENT TEST:\n\n"
            "1. Disconnect the debugger cable from the Bolt PCBA.\n"
            "2. Disconnect the Bolt USB cable from the Raspberry Pi.\n"
            "3. Ensure the PPK2 alligator clips remain connected to the Bolt.\n\n"
            "Press OK once the connections are correct to begin the current measurement."
        )

        label = ttk.Label(popup, text=msg, anchor=W, justify=LEFT)
        label.config(font=("TkFixedFont", 14))
        ok_button = ttk.Button(popup, text="OK", command=acknowledge)
        
        label.grid(column=0, row=0, padx=10, pady=10)
        ok_button.grid(column=0, row=1, padx=10, pady=10)

        self.sleep_current_var.set(0)
        self.wait_variable(self.sleep_current_var)

    # USB re‑plug instructions after flashing test firmware
    def usb_replug_window(self):
        popup = Toplevel(padx=20, pady=20)
        popup.protocol("WM_DELETE_WINDOW", self.disable_event)

        def acknowledge():
            self.usb_replug_var.set(1)
            popup.destroy()
            return
        
        popup.title("Reconnect Bolt USB")

        msg = (
            "USB RECONNECT:\n\n"
            "1. Unplug the Bolt PCBA USB cable from the Raspberry Pi.\n"
            "2. Plug the Bolt PCBA USB cable back into the Raspberry Pi.\n\n"
            "Wait a few seconds for the device to re‑enumerate, then press OK."
        )

        label = ttk.Label(popup, text=msg, anchor=W, justify=LEFT)
        label.config(font=("TkFixedFont", 14))
        ok_button = ttk.Button(popup, text="OK", command=acknowledge)

        label.grid(column=0, row=0, padx=10, pady=10)
        ok_button.grid(column=0, row=1, padx=10, pady=10)

        self.usb_replug_var.set(0)
        self.wait_variable(self.usb_replug_var)
    
    
    # Opens a pop-up prompting the user to scan the Bolt QR / PCBA barcode
    def scan_pcba_barcode_window(self):
        self.pcba_barcode.set(value="")
        self.pcba_barcode_scan_var.set(0)
        scan_confirm_var = IntVar(value=0)
        scan_pcba_barcode_popup = Toplevel(padx=20, pady=20)
        scan_pcba_barcode_popup.protocol("WM_DELETE_WINDOW", self.disable_event)

        def expand_short_bolt_id(raw: str) -> str:
            """Expand short numeric input (e.g. '181') to full format 'Bolt_30000181'."""
            s = raw.strip()
            if not s:
                return s
            if s.isdigit() and 1 <= len(s) <= 3:
                return f"Bolt_30000{s.zfill(3)}"
            return s

        def confirm_barcode():
            raw = self.pcba_barcode.get().strip()
            if raw:
                expanded = expand_short_bolt_id(raw)
                self.pcba_barcode.set(expanded)
                scan_confirm_var.set(1)
                scan_pcba_barcode_popup.destroy()

        def skip_barcode_scan():
            self.pcba_barcode_scan_var.set(1)
            self.pcba_barcode.set(value="")
            scan_confirm_var.set(2)
            scan_pcba_barcode_popup.destroy()

        scan_pcba_barcode_popup.title("PCBA Barcode Scan")

        msg_str = "Please scan the Bolt QR / PCB serial label, or type manually, then click OK"

        ask_to_scan_msg = ttk.Label(scan_pcba_barcode_popup, text=msg_str)
        ask_to_scan_msg.config(font=("TkFixedFont", 16))
        barcode_entry = ttk.Entry(scan_pcba_barcode_popup, textvariable=self.pcba_barcode)
        ok_button = ttk.Button(scan_pcba_barcode_popup, text="OK", command=confirm_barcode)
        skip_button = ttk.Button(scan_pcba_barcode_popup, text="SKIP", command=skip_barcode_scan)

        barcode_entry.bind("<Return>", lambda e: confirm_barcode())

        ask_to_scan_msg.grid(column=0, row=0, columnspan=2)
        barcode_entry.grid(column=0, row=1, columnspan=2, padx=10, pady=10)
        ok_button.grid(column=0, row=2, padx=10, pady=10)
        skip_button.grid(column=1, row=2, padx=10, pady=10)

        barcode_entry.focus()

        self.wait_variable(scan_confirm_var)
        return


    # Opens a popup prompting the user to select if the current ID on the DUT should be replaced.
    def keep_old_id_window(self, id):
        keep_old_id_popup = Toplevel(padx=20, pady=20)
        keep_old_id_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        ## 1 == yes, 2 == no
        def set_new_id():
            self.keep_old_id_var.set(1)
            keep_old_id_popup.destroy()
            return
    
        def no_new_id():
            self.keep_old_id_var.set(2)
            keep_old_id_popup.destroy()
            return
        
        keep_old_id_popup.title("Old ID Detected")
        
        keep_old_id_msg     = ttk.Label(keep_old_id_popup, text=f"Device has already been assigned ID {id}.\nWould you like to assign a new one?")
        keep_old_id_msg.config(font=("TkFixedFont", 16))
        popup_yes_button    = ttk.Button(keep_old_id_popup, text="YES", command=set_new_id)
        popup_no_button     = ttk.Button(keep_old_id_popup, text="NO", command=no_new_id)
        
        keep_old_id_msg.grid(column=0, row=0, columnspan=2)
        popup_yes_button.grid(column=0, row=1, padx=10, pady=10)
        popup_no_button.grid(column=1, row=1, padx=10, pady=10)
        return
    
    
    # Opens a popup prompting the user to select if a new ID should be assigned to the device.
    def write_new_id_window(self):
        write_new_id_popup = Toplevel(padx=20, pady=20)
        write_new_id_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        ## 1 == yes, 2 == no
        def set_new_id():
            self.write_new_id_var.set(1)
            write_new_id_popup.destroy()
            return
    
        def no_new_id():
            self.write_new_id_var.set(2)
            write_new_id_popup.destroy()
            return
        
        write_new_id_popup.title("Write new ID?")
        
        no_id_msg = ttk.Label(write_new_id_popup, text="Device has not been assigned an ID.\nWould you like to assign one?")
        no_id_msg.config(font=("TkFixedFont", 16))
        popup_yes_button = ttk.Button(write_new_id_popup, text="YES", command=set_new_id)
        popup_no_button = ttk.Button(write_new_id_popup, text="NO", command=no_new_id)
        
        no_id_msg.grid(column=0, row=0, columnspan=2)
        popup_yes_button.grid(column=0, row=1, padx=10, pady=10)
        popup_no_button.grid(column=1, row=1, padx=10, pady=10)
        return
    
    
    # Opens a pop-up prompting the user to scan the label on the next available enclosure
    def scan_enclosure_label_barcode_window(self, retry: bool):
        self.enclosure_label.set("")
        scan_enclosure_label_barcode_popup = Toplevel(padx=20, pady=20)
        scan_enclosure_label_barcode_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        scan_enclosure_label_barcode_popup.title("Scan enclosure label")
        
        if retry:
            msg = "Device with this ID already exists on Coiote or Portal. Please try another label."
        else:
            msg = "Scan the label on the next available enclosure."
            
        ask_to_scan_msg = ttk.Label(scan_enclosure_label_barcode_popup, text=msg)
        ask_to_scan_msg.config(font=("TkFixedFont", 16))
        barcode_entry = ttk.Entry(scan_enclosure_label_barcode_popup, textvariable=self.enclosure_label)
        
        ask_to_scan_msg.grid(column=0, row=0, columnspan=2)
        barcode_entry.grid(column=0, row=1, padx=10, pady=10)
        
        barcode_entry.focus()
        
        barcode_scanned = False
        while not barcode_scanned:
            scan_enclosure_label_barcode_popup.update()
            if self.enclosure_label.get() != "":
                time.sleep(0.3)
                scan_enclosure_label_barcode_popup.update()
                self.enclosure_label.get()
                barcode_scanned = True
                
        scan_enclosure_label_barcode_popup.destroy()
    

    # Opens a pop-up prompting the user to select if they would like to add a new device to the portals
    def add_device_to_portal_window(self, id):
        add_dev_to_portal_popup = Toplevel(padx=20, pady=20)
        add_dev_to_portal_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        self.add_dev_to_portal_var.set(0)
        
        ## 1 == yes, 2 == no
        def add_dev():
            self.add_dev_to_portal_var.set(1)
            add_dev_to_portal_popup.destroy()
            return
    
        def do_not_add_dev():
            self.add_dev_to_portal_var.set(2)
            add_dev_to_portal_popup.destroy()
            return
        
        add_dev_to_portal_popup.title("Add to EXACT Portal?")
        
        add_dev_msg = ttk.Label(add_dev_to_portal_popup, text=f"ID {id} found.\nWould you like to add this device to the EXACT Portal?")
        add_dev_msg.config(font=("TkFixedFont", 16))
        popup_yes_button = ttk.Button(add_dev_to_portal_popup, text="YES", command=add_dev)
        popup_no_button = ttk.Button(add_dev_to_portal_popup, text="NO", command=do_not_add_dev)
        
        add_dev_msg.grid(column=0, row=0, columnspan=2)
        popup_yes_button.grid(column=0, row=1, padx=10, pady=10)
        popup_no_button.grid(column=1, row=1, padx=10, pady=10)
        return
    
    
    # Opens a window asking if the user would like to overwrite a device on portal with a new device ID
    def overwrite_device_on_portal_window(self, hw_id, id):
        overwrite_device_popup = Toplevel(padx=20, pady=20)
        overwrite_device_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        self.overwrite_device_var.set(0)

        ## 1 == yes, 2 == no
        def overwrite_device():
            self.overwrite_device_var.set(1)
            overwrite_device_popup.destroy()
            return
    
        def do_not_overwrite_device():
            self.overwrite_device_var.set(2)
            overwrite_device_popup.destroy()
            return
        
        overwrite_device_popup.title("Overwrite Device?")
        
        overwrite_msg = ttk.Label(overwrite_device_popup, text=f"Device urn:dev:mac:{hw_id} already exists on the\n"+
                                  "EXACT Portal and Coiote with a different Device ID.\n"+
                                  f"Would you like to overwrite this device with Device ID {id}?\n"+
                                  "NOTE: THE DEVICE ID MUST BE MANUALLY\n"+
                                  "REASSIGNED ON THE EXACT PORTAL.")
        
        overwrite_msg.config(font=("TkFixedFont", 16))
        popup_yes_button = ttk.Button(overwrite_device_popup, text="YES", command=overwrite_device)
        popup_no_button = ttk.Button(overwrite_device_popup, text="NO", command=do_not_overwrite_device)
        
        overwrite_msg.grid(column=0, row=0, columnspan=2)
        popup_yes_button.grid(column=0, row=1, padx=10, pady=10)
        popup_no_button.grid(column=1, row=1, padx=10, pady=10)
        return
    
    # Opens a pop-up prompting the user to select if they would like to add an existing device to the portals
    def add_preassigned_device_to_portal_window(self, id):
        add_dev_to_portal_popup = Toplevel(padx=20, pady=20)
        add_dev_to_portal_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        self.add_dev_to_portal_var.set(0)
        
        ## 1 == yes, 2 == no
        def add_dev():
            self.add_dev_to_portal_var.set(1)
            add_dev_to_portal_popup.destroy()
            return
    
        def do_not_add_dev():
            self.add_dev_to_portal_var.set(2)
            add_dev_to_portal_popup.destroy()
            return
        
        add_dev_to_portal_popup.title("Add to EXACT Portal?")
        
        add_dev_msg = ttk.Label(add_dev_to_portal_popup, text=f"ID {id} already assigned to board but does not exist\n"+
                                "on EXACT Portal or Coiote. Would you like to add this device?")
        add_dev_msg.config(font=("TkFixedFont", 16))
        popup_yes_button = ttk.Button(add_dev_to_portal_popup, text="YES", command=add_dev)
        popup_no_button = ttk.Button(add_dev_to_portal_popup, text="NO", command=do_not_add_dev)
        
        add_dev_msg.grid(column=0, row=0, columnspan=2)
        popup_yes_button.grid(column=0, row=1, padx=10, pady=10)
        popup_no_button.grid(column=1, row=1, padx=10, pady=10)
        return
    
    
    # Opens a window asking the user if they would like to print a label
    def ask_to_print_label_window(self):
        ask_to_print_label_popup = Toplevel(padx=20, pady=20)
        ask_to_print_label_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        ## 1 == yes, 2 == no
        def set_new_id():
            self.print_label_var.set(1)
            ask_to_print_label_popup.destroy()
            return
    
        def no_new_id():
            self.print_label_var.set(2)
            ask_to_print_label_popup.destroy()
            return
        
        ask_to_print_label_popup.title("Print Label?")
        
        print_label_msg = ttk.Label(ask_to_print_label_popup, text="Device failed test. Would you like to print a label?")
        print_label_msg.config(font=("TkFixedFont", 16))
        popup_yes_button = ttk.Button(ask_to_print_label_popup, text="YES", command=set_new_id)
        popup_no_button = ttk.Button(ask_to_print_label_popup, text="NO", command=no_new_id)
        
        print_label_msg.grid(column=0, row=0, columnspan=2)
        popup_yes_button.grid(column=0, row=1, padx=10, pady=10)
        popup_no_button.grid(column=1, row=1, padx=10, pady=10)
        return
    
    
    # Opens a window notifying the user that a test is complete
    def test_complete_window(self):
        test_complete_popup = Toplevel(padx=20, pady=20)
        test_complete_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        ## 1 == yes, 2 == no
        def acknowledge_test_complete():
            self.test_complete_var.set(1)
            test_complete_popup.destroy()
            return
    
        test_complete_popup.title("")
        
        test_complete_msg = ttk.Label(test_complete_popup, text="Test Completed. Open Lid")
        test_complete_msg.config(font=("TkFixedFont", 16))
        popup_ok_button = ttk.Button(test_complete_popup, text="OK", command=acknowledge_test_complete)

        test_complete_msg.grid(column=0, row=0, columnspan=2)
        popup_ok_button.grid(column=0, row=1, padx=10, pady=10)
        return
    
    
    # Opens a pop-up notifying the user the printer was not correctly plugged in and prompts to try again
    def failed_print_window(self):
        failed_print_popup = Toplevel(padx=20, pady=20)
        failed_print_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        ## 1 == yes, 2 == no
        def retry_print():
            self.print_again_var.set(1)
            failed_print_popup.destroy()
            return
    
        def skip_print():
            self.print_again_var.set(2)
            failed_print_popup.destroy()
            return
        
        failed_print_popup.title("Add to EXACT Portal?")
        
        print_failed_msg = ttk.Label(failed_print_popup, text="Print failed. Confirm the printer is powered on.\nWould you like to try again?")
        print_failed_msg.config(font=("TkFixedFont", 16))
        popup_yes_button = ttk.Button(failed_print_popup, text="YES", command=retry_print)
        popup_no_button = ttk.Button(failed_print_popup, text="NO", command=skip_print)
        
        print_failed_msg.grid(column=0, row=0, columnspan=2)
        popup_yes_button.grid(column=0, row=1, padx=10, pady=10)
        popup_no_button.grid(column=1, row=1, padx=10, pady=10)
        return
    
    # Opens a popup instructing the operator to restart the fixture application
    def restart_fixture_window(self):
        restart_popup = Toplevel(padx=20, pady=20)
        restart_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        def acknowledge():
            self.restart_fixture_var.set(1)
            restart_popup.destroy()
            return
        
        restart_popup.title("PPK2 Abnormal Reading Detected")
        
        msg = (
            "ABNORMAL PPK2 READING DETECTED:\n\n"
            "The sleep current measurement shows abnormally high readings\n"
            "(> 1000 uA), which indicates a PPK2/fixture issue rather than\n"
            "a board problem.\n\n"
            "Please close this application and restart the fixture.\n\n"
            "Press OK to exit the application."
        )
        
        label = ttk.Label(restart_popup, text=msg, anchor=W, justify=LEFT)
        label.config(font=("TkFixedFont", 14))
        ok_button = ttk.Button(restart_popup, text="OK", command=acknowledge)
        
        label.grid(column=0, row=0, padx=10, pady=10)
        ok_button.grid(column=0, row=1, padx=10, pady=10)
        
        self.restart_fixture_var.set(0)
        self.wait_variable(self.restart_fixture_var)
    
    # Opens a popup asking the operator to confirm rebooting the Raspberry Pi
    def reboot_pi_window(self) -> bool:
        """
        Show a confirmation dialog for rebooting the Raspberry Pi.
        
        Returns:
            True if the operator confirmed the reboot, False otherwise
        """
        reboot_popup = Toplevel(padx=20, pady=20)
        reboot_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        def confirm_reboot():
            self.reboot_pi_var.set(1)
            reboot_popup.destroy()
            return
        
        def cancel_reboot():
            self.reboot_pi_var.set(2)
            reboot_popup.destroy()
            return
        
        reboot_popup.title("Reboot Raspberry Pi?")
        
        msg = (
            "REPEATED PPK2 ABNORMAL READINGS:\n\n"
            "The PPK2 has shown abnormal readings multiple times.\n"
            "Restarting the fixture did not resolve the issue.\n\n"
            "Would you like to reboot the Raspberry Pi?\n\n"
            "This will close the application and restart the system."
        )
        
        label = ttk.Label(reboot_popup, text=msg, anchor=W, justify=LEFT)
        label.config(font=("TkFixedFont", 14))
        yes_button = ttk.Button(reboot_popup, text="YES", command=confirm_reboot)
        no_button = ttk.Button(reboot_popup, text="NO", command=cancel_reboot)
        
        label.grid(column=0, row=0, columnspan=2, padx=10, pady=10)
        yes_button.grid(column=0, row=1, padx=10, pady=10)
        no_button.grid(column=1, row=1, padx=10, pady=10)
        
        self.reboot_pi_var.set(0)
        self.wait_variable(self.reboot_pi_var)
        
        # Return True if confirmed (value 1), False otherwise
        return self.reboot_pi_var.get() == 1
    
    # Opens a popup instructing the operator to restart the test after first BLE failure
    def ble_retry_window(self):
        """
        Show a popup informing the operator that the BLE test failed for the first time
        (likely due to transient condition like re-power/advertising name change) and
        instruct them to restart the test for the same board.
        """
        ble_retry_popup = Toplevel(padx=20, pady=20)
        ble_retry_popup.protocol("WM_DELETE_WINDOW", self.disable_event)
        
        def acknowledge():
            self.ble_retry_var.set(1)
            ble_retry_popup.destroy()
            return
        
        ble_retry_popup.title("BLE Test First Failure")
        
        msg = (
            "BLE TEST FIRST FAILURE:\n\n"
            "The BLE test failed, likely due to a transient condition\n"
            "(re-power or temporary advertising name change).\n\n"
            "Please restart the test for the same board.\n\n"
            "Press OK to continue."
        )
        
        label = ttk.Label(ble_retry_popup, text=msg, anchor=W, justify=LEFT)
        label.config(font=("TkFixedFont", 14))
        ok_button = ttk.Button(ble_retry_popup, text="OK", command=acknowledge)
        
        label.grid(column=0, row=0, padx=10, pady=10)
        ok_button.grid(column=0, row=1, padx=10, pady=10)
        
        self.ble_retry_var.set(0)
        self.wait_variable(self.ble_retry_var)
    
    
if __name__ == "__main__":
    gui = App()
    while True:
        gui.update_window()