import os
import sys
from dotenv import load_dotenv

load_dotenv()
import time
import unicodedata
import datetime
import shutil
import argparse
import pickle
import json
import re
from typing import List, Optional

import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError
import google.generativeai as genai
from git import Repo
import io

# SCOPES for Google Drive
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

# Function to get credentials
def get_credentials():
    creds = None
    # The file token.pickle stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if os.path.exists('credentials.json'):
                # Check if it's a service account or client secret
                with open('credentials.json', 'r') as f:
                    data = json.load(f)
                
                if 'type' in data and data['type'] == 'service_account':
                   creds = service_account.Credentials.from_service_account_file(
                        'credentials.json', scopes=SCOPES)
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        'credentials.json', SCOPES)
                    creds = flow.run_local_server(port=0)
                    # Save the credentials for the next run
                    with open('token.pickle', 'wb') as token:
                        pickle.dump(creds, token)
            else:
                print("Error: credentials.json not found.")
                sys.exit(1)
    return creds

# Function to list files in a folder
def list_files(service, folder_id):
    files = []
    page_token = None
    while True:
        try:
            response = service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                spaces='drive',
                fields='nextPageToken, files(id, name, mimeType)',
                pageToken=page_token
            ).execute()
            files.extend(response.get('files', []))
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
        except HttpError as error:
            print(f'An error occurred: {error}')
            break
    return files

# Function to download file content
def download_file_content(service, file_id, mime_type):
    try:
        if mime_type == 'application/vnd.google-apps.document':
            request = service.files().export_media(fileId=file_id, mimeType='text/plain')
        elif mime_type.startswith('text/'):
             request = service.files().get_media(fileId=file_id)
        else:
            print(f"Skipping unsupported mime type: {mime_type}")
            return None

        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        
        return fh.getvalue().decode('utf-8')
    except HttpError as error:
        print(f'An error occurred downloading file: {error}')
        return None

# Sorting Order
ORDER_LIST = [
    # Month
    "月1", "月2", "月3", "月4", "月5", "月6",
    # Fire
    "火1", "火2", "火3", "火4", "火5", "火6", "火7",
    # Water
    "水1", "水2", "水3", "水4", "水5", "水6", "水7",
    # Wood
    "木1", "木2", "木3", "木4", "木5", "木6", "木7",
    # Gold
    "金1", "金2", "金3", "金4", "金5", "金6", "金7",
    # Earth
    "土1", "土2"
]
ORDER_MAP = {name: i + 1 for i, name in enumerate(ORDER_LIST)}


def get_sort_key(filename):
    # Normalize filename first
    normalized_name = unicodedata.normalize('NFKC', filename)
    # Extract the part that matches the order list
    # Assuming filename is just "月1" or contains it. Prompt says file content is irrelevant, filename contains only frame name.
    # But files might have extensions or subtle differences.
    # Let's try to match exactly or prefix.
    # Actually prompt says: "ファイル名には「月1」「火2」などの枠名しか書かれていません"
    
    # Remove extension if any for checking
    name_no_ext = os.path.splitext(normalized_name)[0]
    
    if name_no_ext in ORDER_MAP:
        return ORDER_MAP[name_no_ext], name_no_ext
    
    # Try fuzzy match if exact match fails
    for key in ORDER_MAP:
        if key in normalized_name:
             return ORDER_MAP[key], key
             
    return 999, normalized_name

def generate_report(content, target_month):
    # Load system prompt
    try:
        with open('gemini_prompt.txt', 'r', encoding='utf-8') as f:
            system_prompt = f.read()
    except FileNotFoundError:
        print("Error: gemini_prompt.txt not found.")
        sys.exit(1)

    user_instruction = f"""
以下のデータから、**{target_month} の日付**に該当する記録のみを抽出して報告書を作成してください。
他の月の記録は無視してください。
もし対象月の記録が一切ない場合は、『記録なし』とだけ出力してください。

【ドキュメントデータ】
{content}
"""
    
    # Configure Gemini
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable not set.")
        # Ask user for input if not set? Or assume it's set. 
        # The prompt says "Use environment variable (or input)".
        # let's try input if env is missing
        api_key = input("Please enter your Gemini API Key: ").strip()
        
    genai.configure(api_key=api_key)
    
    # User preference: Prioritize 3.1 Pro, fallback to 3.0 Pro after 10 mins
    target_models = ['gemini-3.1-pro-preview', 'gemini-3-pro-preview']
    
    start_time = time.time()
    response_text = None
    
    for model_name in target_models:
        print(f"Generating with {model_name}...")
        model = genai.GenerativeModel(model_name, system_instruction=system_prompt)
        
        while True:
            try:
                # Calculate elapsed time for current file processing
                elapsed_time = time.time() - start_time
                
                # If we are on the first model (3.1 Pro) and exceeded 10 minutes (600 seconds)
                if model_name == target_models[0] and elapsed_time > 600:
                    print(f"  -> Timeout (10 mins) exceeded for {model_name}. Switching to fallback model.")
                    break # Break inner while loop to move to next model in for loop
                
                response = model.generate_content(user_instruction)
                response_text = response.text
                return response_text
                
            except Exception as e:
                if "429" in str(e) or "Quota exceeded" in str(e) or "Resource has been exhausted" in str(e):
                    # Rate limit hit
                    wait_time = 60
                    print(f"  -> Rate limit exceeded for {model_name}. Waiting {wait_time} seconds... (Elapsed: {int(time.time() - start_time)}s)")
                    time.sleep(wait_time)
                    continue
                else:
                    # Other errors (e.g. model not found, internal error) -> Skip to next model immediately
                    print(f"Error with {model_name}: {e}")
                    break 
        
        # If we successfully got a response, return it (handled inside try block).
        # If we broke out of while loop (timeout or non-retriable error), we continue to next model.
            
    return response_text

def main():
    parser = argparse.ArgumentParser(description='Generate monthly reports.')
    args = parser.parse_args()

    # User Inputs
    print("--- Monthly Report Generator ---")
    # folder_id = input("Enter Google Drive Folder ID: ").strip()
    # target_month = input("Enter Target Month (e.g., 2026年2月): ").strip()
    folder_id = "13UGFVMSlukZofnjxTe1HWXBdwCsFRfer"
    target_month = "2026-01"
    
    if not folder_id or not target_month:
        print("Folder ID and Target Month are required.")
        sys.exit(1)

    # Auth
    creds = get_credentials()
    service = build('drive', 'v3', credentials=creds)

    # List Files
    print(f"Listing files in folder {folder_id}...")
    files = list_files(service, folder_id)
    print(f"Found {len(files)} files.")

    # Prepare Output Directory
    output_dir = os.path.join(os.getcwd(), target_month)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created output directory: {output_dir}")

    # Process Files
    # First, sort files to ensure order
    # Sort files based on the simplified name logic
    files_with_sort_info = []
    for file in files:
        normalized_name = unicodedata.normalize('NFKC', file['name'])
        sort_order, logic_name = get_sort_key(normalized_name)
        files_with_sort_info.append({
            'file': file,
            'sort_order': sort_order,
            'logic_name': logic_name,
            'normalized_name': normalized_name
        })
    
    # Sort files
    files_with_sort_info.sort(key=lambda x: x['sort_order'])

    # Scan existing files
    existing_logic_names = set()
    current_max_prefix = 0
    
    if os.path.exists(output_dir):
        for filename in os.listdir(output_dir):
            if filename.endswith(".md"):
                # Expecting format: NN_LogicName.md
                match = re.match(r"^(\d+)_(.+)\.md$", filename)
                if match:
                    prefix_num = int(match.group(1))
                    logic_name_extracted = match.group(2)
                    existing_logic_names.add(logic_name_extracted)
                    if prefix_num > current_max_prefix:
                        current_max_prefix = prefix_num

    # Determine reporting counter start
    report_count = current_max_prefix
    
    for item in files_with_sort_info:
        file = item['file']
        sort_order = item['sort_order']
        logic_name = item['logic_name']
        
        if sort_order == 999:
            print(f"Skipping file with unknown frame name: {file['name']}")
            continue

        # Check if logic name already processed
        if logic_name in existing_logic_names:
             print(f"Skipping already processed frame: {logic_name}")
             continue
             
        # Check if file already exists to skip re-generation (filename check backup)
        # Construct expected filename - wait, we don't know the exact filename if skipped.
        # But we checked logic_name, so we are good.

        print(f"Processing: {logic_name} (ID: {file['id']})")
        
        # Download Content
        content = download_file_content(service, file['id'], file['mimeType'])
        if not content:
            print("  -> Could not download content.")
            continue
            
        # Generate Report
        report = generate_report(content, target_month)
        
        if not report:
            print("  -> Generation failed.")
            continue
            
        if "記録なし" in report:
            print("  -> No records found (Gemini returned '記録なし'). Skipping.")
            continue
            
        # Determine Filename (Logic moved up, reuse variables)
        # report_count += 1
        # prefix = f"{report_count:02d}"
        # filename = f"{prefix}_{logic_name}.md"
        # filepath = os.path.join(output_dir, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"  -> Saved to {filepath}")
        
        # Rate Limiting
        print("  -> Sleeping for 15 seconds...")
        time.sleep(15)

    # Git Operations
    print("Performing Git operations...")
    try:
        repo = Repo(os.getcwd())
        if repo.is_dirty(untracked_files=True):
            repo.git.add('.')
            commit_message = f"Add reports for {target_month}"
            repo.git.commit('-m', commit_message)
            print(f"  -> Committed: {commit_message}")
            
            origin = repo.remote(name='origin')
            print("  -> Pushing to origin...")
            # origin.push()
            repo.git.push('--set-upstream', 'origin', repo.active_branch.name)
            print("  -> Push completed.")
        else:
            print("  -> No changes to commit.")
            
    except Exception as e:
        print(f"Git Error: {e}")

if __name__ == '__main__':
    main()
