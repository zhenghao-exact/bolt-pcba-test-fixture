import os
import csv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
import datetime
import json

SCOPES = ['https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/drive.metadata',
    'https://www.googleapis.com/auth/spreadsheets']

TEAM_DRIVE_ID = '0AHfLcRSZ2qDHUk9PVA'
TEST_RESULTS_FOLDER_ID = '1WOJNzdR4okeNV8mQODa4qVa8G1uv8bH6'
# FIRMWARE_FOLDER_ID = '1lQRLi2DC8lDLPJ-NyCJVzz-7HJC1iPIT'
# PRODUCTION_FOLDER_PATH = "/home/yu/git/exact/bolt-pcba-test-fixture/bolt-pcba-test-fixture/fw"            # Path to production FW folder

#change directory to working directory
abspath = os.path.abspath(__file__)
dname = os.path.dirname(abspath)
os.chdir(dname)

KEY_FILE = os.getcwd() + '/fixture-test-results-689646566465.json'

# New file is created each fiscal year
create_new_drive_file = False

# Bolt fixture ID is always 1
fixture_id = 1

# Current FW version
latest_fw_version = ""

# Get month and year for data storage
date = datetime.date.today()
year = date.year
month = date.month

# Write results in CSV by fiscal year
if date.month > 6:
    year = year + 1
    
local_results_filepath = f"/home/boltfixturepi/Documents/bolt-pcba-test-fixture/{year}.csv"

# Open json file and get last year and drive file id
with open("fixture_config.json", "r") as json_file:
    data = json.load(json_file) 
    
json_data = {
    "fixtures" : [{
        "fixture_id" : data["fixtures"][0]["fixture_id"],
        "drive_id" : data["fixtures"][0]["drive_id"],
        "year" : data["fixtures"][0]["year"],
        "rsrp" : data["fixtures"][0]["rsrp"]
    },{
        "fixture_id" : data["fixtures"][1]["fixture_id"],
        "drive_id" : data["fixtures"][1]["drive_id"],
        "year" : data["fixtures"][1]["year"],
        "rsrp" : data["fixtures"][1]["rsrp"]
    }]
}
    
print(json_data)    

def authenticate():
    credentials = service_account.Credentials.from_service_account_file(KEY_FILE, scopes=SCOPES)
    sheets_service = build('sheets', 'v4', credentials=credentials)
    drive_service = build('drive', 'v3', credentials=credentials)
    return sheets_service, drive_service

def create_new_sheet_on_drive(sheets_service, drive_service):
    
    file_metadata = {
        'name' : f"Bolt PCBA Test Fixture {fixture_id} Results {year}",
        'mimeType' : 'application/vnd.google-apps.spreadsheet',
        'parents': [TEST_RESULTS_FOLDER_ID],  # Specify the folder ID within the shared drive
        'driveId': TEAM_DRIVE_ID,  # Specify the shared drive ID
    }
    
    file = drive_service.files().create(
        body = file_metadata, 
        fields='id',
        supportsAllDrives=True
    ).execute()
    
    sheet_id = file.get('id')
    print(sheet_id)

    json_data["fixtures"][fixture_id-1]["drive_id"] = sheet_id
    # Write new file information to json file after initial file upload
    json_object = json.dumps(json_data)
    with open("fixture_config.json", "w") as json_file:
        json_file.write(json_object)

    return sheet_id
    
# If a new year is detected, create a new file in google drive next upload
if int(year) != int(json_data["fixtures"][fixture_id-1]["year"]):
    print("New fiscal year detected!")
    json_data["fixtures"][fixture_id-1]["year"] = year
    sheet_service, drive_service = authenticate()
    create_new_sheet_on_drive(sheet_service, drive_service)
    
# Get current FW version from JSON file (optional - only needed for firmware checking)
try:
    with open("device_fw.json", "r") as json_file:
        data = json.load(json_file)
    latest_fw_version = data["version"]
except FileNotFoundError:
    # Bolt doesn't use firmware checking, so this file is optional
    latest_fw_version = ""

def update_sheet_on_drive(sheets_service, sheet_id):
    # Read CSV content
    with open(local_results_filepath, 'r') as csv_file:
        csv_reader = csv.reader(csv_file)
        csv_data = list(csv_reader)

    # Prepare request body
    body = {
        'values': csv_data
    }
    
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id, 
        range='A1', 
        valueInputOption='RAW', 
        body=body,
    ).execute()

def upload_to_drive():
    """
    Sync local fiscal-year CSV to Google Sheets. Rows are produced by csv_manager;
    the Sleep Current Test column may be True, False, or 'skipped' when the
    operator skipped sleep measurement in the GUI.
    """
    try:
        sheet_service, drive_service = authenticate()
        update_sheet_on_drive(sheet_service, json_data["fixtures"][fixture_id-1]["drive_id"])
    except:
        print("Failed to update sheet on drive.")
        
    return True

def check_for_fw():
    sheet_service, drive_service = authenticate()
    # Get all files within FW directory
    try:
        results = drive_service.files().list(q = "'" + FIRMWARE_FOLDER_ID + "' in parents", pageSize=10, fields="nextPageToken, files(id, name)", supportsAllDrives = True, includeItemsFromAllDrives=True).execute()
        items = results.get('files', [])
        fw_number = int(latest_fw_version.replace('.', '')) # Raw integer version number
    
        for item in items:
            name = item["name"]
            # Get version number from filename
            try:
                start_index = name.index("v")
                end_index = name.index("_", start_index)
                remote_version = name[start_index+1:end_index]
                remote_fw_number = int(remote_version.replace('.',''))
            except:
                # If unable to parse filename, skip file
                remote_fw_number = 0
                
            # Download new FW version if available
            if remote_fw_number != fw_number and remote_fw_number != 0:
                print(f"New FW v{remote_version} found. Downloading...")
                request = drive_service.files().get_media(fileId=item["id"])
                file = io.FileIO(f"{PRODUCTION_FOLDER_PATH}{item['name']}", 'wb') 
                downloader = MediaIoBaseDownload(file, request)
                
                done = False
                while done is False:
                    status, done = downloader.next_chunk()
                    print(f"Download {int(status.progress() * 100)}.")
                    
                print("FW Download complete")
                    
                # Save new FW version and filename to JSON file
                json_fw = {
                    "version" : remote_version,
                    "filename" : name
                }
                json_object = json.dumps(json_fw)
                with open("device_fw.json", "w") as json_file:
                    json_file.write(json_object)
                
                json_file.close()
                    
                # Update file path
                return True
    except:
        print("Issue with searching for new FW file. Check internet connection.")
        
    return False

if __name__ == "__main__":
    create_new_drive_file = True
    upload_to_drive()