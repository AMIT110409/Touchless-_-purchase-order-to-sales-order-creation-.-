import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import json
import time
import base64
import tempfile
import traceback
from azure.identity import DefaultAzureCredential
from azure.storage.queue import QueueClient
from azure.storage.blob import BlobServiceClient
from smart_po_extraction import process_file, setup_llm
from pathlib import Path

# Config
QUEUE_URL = os.getenv("AZURE_QUEUE_URL") # https://<account>.queue.core.windows.net/po-processing-queue
BLOB_URL = os.getenv("AZURE_BLOB_URL")   # https://<account>.blob.core.windows.net/
INPUT_CONTAINER = "input-po"
OUTPUT_CONTAINER = "processed-json"
FAILED_CONTAINER = "failed"
POLL_INTERVAL = 5

def main():
    print("Starting Azure Worker...")
    
    # Authenticate (Managed Identity in Cloud, DefaultAzureCredential locally)
    credential = DefaultAzureCredential()
    
    #Clients
    queue_client = QueueClient.from_queue_url(QUEUE_URL, credential=credential)
    blob_service_client = BlobServiceClient(account_url=BLOB_URL, credential=credential)
    
    # Initialize LLM only once
    setup_llm()
    
    print(f"Listening on {QUEUE_URL}...")
    
    while True:
        try:
            # Get messages (visibility timeout 10 mins to allow processing)
            messages = queue_client.receive_messages(messages_per_page=1, visibility_timeout=600)
            
            for msg in messages:
                print(f"Received message: {msg.id}")
                content = msg.content
                
                # Decode if base64 (Event Grid sometimes sends base64, sometimes JSON)
                try:
                    # Basic check if it looks like JSON or just a path
                    # For simplicity, let's assume the message IS the file path or a simple JSON having 'url' or 'path'
                    # Or it's an Event Grid event.
                    
                    # Strategy: Try to parse as JSON, look for 'data'->'url' (Event Grid blob created) 
                    # OR just treat body as filename if simple
                    
                    file_name = None
                    try:
                        body_str = content
                        event = json.loads(body_str)
                        # Check typical Event Grid Blob Created schema
                        if isinstance(event, dict) and 'data' in event and 'url' in event['data']:
                            blob_uri = event['data']['url']
                            file_name = blob_uri.split('/')[-1]
                        elif 'filename' in event:
                            file_name = event['filename']
                    except:
                        # Maybe it is just the filename string
                        file_name = content
                        
                    if not file_name:
                         print("Could not parse filename from message. Deleting.")
                         queue_client.delete_message(msg)
                         continue
                         
                    print(f"Processing File: {file_name}")
                    
                    # 1. Download File
                    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file_name).suffix) as tmp:
                        tmp_path = Path(tmp.name)
                    
                    blob_client = blob_service_client.get_blob_client(container=INPUT_CONTAINER, blob=file_name)
                    with open(tmp_path, "wb") as f:
                        download_stream = blob_client.download_blob()
                        f.write(download_stream.readall())
                        
                    # 2. Process
                    print(f"Downloaded to {tmp_path}")
                    result = process_file(tmp_path)
                    
                    # 3. Upload Result
                    result_json = json.dumps(result, indent=2)
                    output_blob_name = f"{Path(file_name).stem}_result.json"
                    output_client = blob_service_client.get_blob_client(container=OUTPUT_CONTAINER, blob=output_blob_name)
                    output_client.upload_blob(result_json, overwrite=True)
                    print(f"Uploaded result to {output_blob_name}")
                    
                    # 4. Cleanup
                    queue_client.delete_message(msg)
                    os.remove(tmp_path)
                    print("Message deleted and temp file cleaned.")
                    
                except Exception as e:
                    print(f"Error processing message {msg.id}: {e}")
                    traceback.print_exc()
                    # Optionally move original file to failed container
                    # queue_client.delete_message(msg) # Or let it reappear to retry?
                    
        except Exception as e:
            print(f"Loop error: {e}")
            
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
