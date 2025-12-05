import brother_ql
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send
from PIL import Image, ImageDraw, ImageFont
import qrcode
import subprocess
import time

# Pillow 10+ removed ANTIALIAS/BILINEAR/BICUBIC; provide compatibility for brother_ql
try:
    from PIL.Image import Resampling as _Resampling
    if not hasattr(Image, "ANTIALIAS"):
        Image.ANTIALIAS = _Resampling.LANCZOS
    if not hasattr(Image, "BICUBIC"):
        Image.BICUBIC = _Resampling.BICUBIC
    if not hasattr(Image, "BILINEAR"):
        Image.BILINEAR = _Resampling.BILINEAR
except Exception:
    pass

print("Printer: starting printer service...")
process = None
try:
    # Use sudo -S to read password from stdin
    process = subprocess.Popen(
        ["sudo", "-S", "chmod", "777", "/dev/usb/lp0"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(input="123456\n", timeout=5.0)
    if process.returncode == 0:
        print("Printer: printer service started successfully")
        time.sleep(2.0)
    else:
        print(f"Printer: printer service start failed: {stderr}")
except subprocess.TimeoutExpired:
    print("Printer: printer service start timed out")
    if process:
        process.kill()  
except Exception as exc:
    print(f"Printer: error starting printer service: {exc}")
    if process:
        try:
            process.kill()
        except Exception:
            pass

font1 = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoMono-Regular.ttf", size = 30)
font2 = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoMono-Regular.ttf", size = 15)

model = 'QL-800'
printer = '/dev/usb/lp0'
backend = 'pyusb'

def print_label(success, measurements, refurb, work_order) -> bool:
    if success:
        success_str = "PASS"
    else: success_str = "FAIL"
    
    qlr = brother_ql.raster.BrotherQLRaster(model)
    qlr.exception_on_warning = True
    
    im = Image.new(mode="1", size=(300,100), color=1)
    
    I1 = ImageDraw.Draw(im)
    I1.text((5,5), success_str, fill=0, font=font1)
    I1.text((5, 40), measurements["HW_ID"], fill=0, font=font2)
    if measurements["dev_ID"] != 0 and success:
        I1.text((5, 60), str(measurements["dev_ID"]), fill=0, font=font2)
    elif not success:
        I1.text((5, 60), str(work_order), fill=0, font=font2)
    if refurb:
        I1.text((5, 80), "REFURB: [_]", fill=0, font=font2)
        
    qr_code_img = qrcode.make(measurements['dev_ID'])
    qr_code_img = qr_code_img.resize((110, 110))
    im.paste(qr_code_img, (180, -5))

    instructions = convert(
        qlr=qlr, 
        images=[im],    #  Takes a list of file names or PIL objects.
        label='12', 
        rotate='90',    # 'Auto', '0', '90', '270'
        threshold=70.0,    # Black and white threshold in percent.
        dither=False, 
        compress=False, 
        red=False,    # Only True if using Red/Black 62 mm label tape.
        dpi_600=False, 
        hq=True,    # False for low quality.
        cut=True
    )
    
    try:
        send(instructions=instructions, printer_identifier=printer, backend_identifier='linux_kernel', blocking=True)
        return True
    except Exception as exc:
        print(f"Could not communicate with printer: {exc}")
        print("Is it plugged in and turned on?")
        return False
    
if __name__ == "__main__":
    import os
    
    print("Testing printer_manager.py")
    print(f"Printer path: {printer}")
    print(f"Printer exists: {os.path.exists(printer)}")
    
    # Test with Bolt-style measurements
    measurements = {
        "HW_ID": "Bolt",
        "dev_ID": "30000097",  # Example Bolt ID
        "test_ID": "1700000000"
    }
    
    print("\nAttempting to print test label...")
    print(f"  HW_ID: {measurements['HW_ID']}")
    print(f"  dev_ID: {measurements['dev_ID']}")
    print(f"  Success: PASS")
    
    result = print_label(True, measurements=measurements, refurb=False, work_order="")
    
    if result:
        print("\n✓ Label printed successfully!")
    else:
        print("\n✗ Label printing failed")
        print("\nTroubleshooting:")
        print(f"  1. Check if printer is powered on")
        print(f"  2. Verify printer path: {printer}")
        print(f"  3. Check USB connection")
        print(f"  4. Try: ls -l {printer}")
        print(f"  5. Check permissions: ls -l /dev/usb/")
    