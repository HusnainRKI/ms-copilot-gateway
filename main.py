import asyncio
import json
import subprocess
import websockets
import time
import platform
import os
import sys # Added for stdin/stdout
import tempfile # Added for temporary directory
import urllib.request # Added for fetching version info
import urllib.error   # Added for URLError exception

# --- Settings ---
# Modify the Edge path according to your environment
EDGE_PATH = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
# Debugging profile directory.
# Defaults to a temporary directory. Set to None to use the default Edge profile.
# DEBUG_PROFILE_DIR = None # Example: Use default profile
DEBUG_PROFILE_DIR = os.path.join(tempfile.gettempdir(), "edge_debug_profile_temp")
DEBUGGING_PORT = 9222
COPILOT_URL = "https://copilot.microsoft.com/"
WEBSOCKET_URL_FILTER = "wss://copilot.microsoft.com/c/api/chat?api-version=2"
# TEST_MESSAGE = "This is an automated test message." # Will be replaced by stdin
# Selector modification may be required if Copilot's UI structure changes
USER_INPUT_SELECTOR = "textarea#userInput"
# Submit button selector (simple version)
SUBMIT_BUTTON_SELECTOR = 'button[data-testid="submit-button"]'

# --- CDP Related ---
_cdp_message_id = 0

async def send_cdp_command(ws, method, params={}, session_id=None):
    """Send a CDP command and return the message ID"""
    global _cdp_message_id
    _cdp_message_id += 1
    msg = {"id": _cdp_message_id, "method": method, "params": params}
    if session_id:
        msg["sessionId"] = session_id
    # print(f"Sending CDP: {msg}") # For debugging
    await ws.send(json.dumps(msg))
    return _cdp_message_id

async def find_page_target(ws):
    """Find the first available page target ID"""
    list_targets_id = await send_cdp_command(ws, "Target.getTargets")
    while True:
        try:
            message = await asyncio.wait_for(ws.recv(), timeout=5.0) # Timeout setting
            data = json.loads(message)
            if data.get("id") == list_targets_id and "result" in data:
                targets = data["result"]["targetInfos"]
                for target in targets:
                    # Exclude devtools itself, etc.
                    if target.get("type") == "page" and not target.get("url", "").startswith("devtools://"):
                        print(f"Found target: {target['targetId']} - {target.get('url')}")
                        return target["targetId"]
                print("Suitable page target not found.")
                return None
            elif "error" in data and data.get("id") == list_targets_id:
                print(f"Error getting targets: {data['error']}")
                return None
        except asyncio.TimeoutError:
            print("Timeout waiting for target list response.")
            return None
        except Exception as e:
            print(f"Error receiving message: {e}")
            return None


async def attach_to_target(ws, target_id):
    """Attach to the specified target ID and return the session ID"""
    attach_cmd_id = await send_cdp_command(ws, "Target.attachToTarget", {"targetId": target_id, "flatten": True})
    while True:
        try:
            message = await asyncio.wait_for(ws.recv(), timeout=10.0) # Timeout setting
            data = json.loads(message)
            # Get session ID from command response
            if data.get("id") == attach_cmd_id and "result" in data:
                session_id = data["result"]["sessionId"]
                print(f"Attached via command response. Session ID: {session_id}")
                return session_id
            # Get session ID from event (event might arrive before response)
            elif data.get("method") == "Target.attachedToTarget":
                if data.get("params", {}).get("targetInfo", {}).get("targetId") == target_id:
                    session_id = data.get("params", {}).get("sessionId")
                    print(f"Attached via event. Session ID: {session_id}")
                    return session_id
            elif "error" in data and data.get("id") == attach_cmd_id:
                 print(f"Error attaching to target: {data['error']}")
                 return None
        except asyncio.TimeoutError:
            print("Timeout waiting for attach response/event.")
            return None
        except Exception as e:
            print(f"Error receiving message during attach: {e}")
            return None

async def monitor_copilot_interaction(ws, session_id, user_message: str):
    """Interact with the Copilot page and monitor WebSocket communication, yielding responses."""
    # Enable necessary CDP domains
    await send_cdp_command(ws, "Page.enable", {}, session_id=session_id)
    await send_cdp_command(ws, "Network.enable", {}, session_id=session_id)
    await send_cdp_command(ws, "Runtime.enable", {}, session_id=session_id)
    await send_cdp_command(ws, "DOM.enable", {}, session_id=session_id) # Enable DOM domain
    print("CDP Domains enabled (Page, Network, Runtime, DOM).")

    # Open Copilot
    print(f"Navigating to {COPILOT_URL}...")
    await send_cdp_command(ws, "Page.navigate", {"url": COPILOT_URL}, session_id=session_id)

    # Wait for page load (shortened)
    print("Waiting for page load (3s)...")
    await asyncio.sleep(3) # Reduced wait time

    # --- Simulate typing the user message using CDP Input domain ---
    print(f"Simulating typing: '{user_message}'")
    # 1. Get document root node ID
    doc_root_id = await send_cdp_command(ws, "DOM.getDocument", {"depth": -1}, session_id=session_id)
    root_node_id = None
    # Need to receive messages until we get the response for doc_root_id
    while root_node_id is None:
        try:
            message = await asyncio.wait_for(ws.recv(), timeout=5.0)
            data = json.loads(message)
            if data.get("id") == doc_root_id:
                if "result" in data:
                    root_node_id = data["result"]["root"]["nodeId"]
                    print("Got document root node ID.")
                elif "error" in data:
                    print(f"Error getting document root: {data['error']}")
                break # Exit loop once response is received (success or error)
        except asyncio.TimeoutError:
            print("Timeout waiting for document root response.")
            break
        except Exception as e:
            print(f"Error receiving message while waiting for document root: {e}")
            break

    if not root_node_id:
        print("Could not get document root node ID. Skipping input simulation.")
    else:
        # 2. Find the textarea node ID
        print(f"Querying for selector: {USER_INPUT_SELECTOR}")
        query_selector_id = await send_cdp_command(ws, "DOM.querySelector", {"nodeId": root_node_id, "selector": USER_INPUT_SELECTOR}, session_id=session_id)
        textarea_node_id = None
        # Need to receive messages until we get the response for query_selector_id
        while textarea_node_id is None: # Loop until we get a result or error
             try:
                 message = await asyncio.wait_for(ws.recv(), timeout=5.0)
                 data = json.loads(message)
                 if data.get("id") == query_selector_id:
                     if "result" in data:
                         textarea_node_id = data["result"]["nodeId"]
                         if textarea_node_id == 0: # querySelector returns 0 if not found
                             print(f"Could not find textarea element with selector: {USER_INPUT_SELECTOR}")
                             textarea_node_id = None # Ensure it's None if not found
                         else:
                             print("Found textarea node ID.")
                     elif "error" in data:
                         print(f"Error querying selector '{USER_INPUT_SELECTOR}': {data['error']}")
                     break # Exit loop once response is received
             except asyncio.TimeoutError:
                 print("Timeout waiting for querySelector response.")
                 break
             except Exception as e:
                 print(f"Error receiving message while waiting for querySelector: {e}")
                 break

        if not textarea_node_id:
            print("Could not find textarea node ID. Skipping input simulation.")
        else:
            # 3. Focus the textarea
            print("Focusing textarea...")
            focus_id = await send_cdp_command(ws, "DOM.focus", {"nodeId": textarea_node_id}, session_id=session_id)
            # Wait briefly for focus command response/potential events, though not strictly necessary to block
            await asyncio.sleep(0.5)

            # 4. Insert text using Input.insertText
            print("Inserting text...")
            insert_text_id = await send_cdp_command(ws, "Input.insertText", {"text": user_message}, session_id=session_id)
            print("Text insertion command sent.")
            # Wait for text insertion to process and UI to potentially update
            await asyncio.sleep(1)

    # --- Click the submit button (Shadow DOM handling) ---
    print("Clicking submit button...")
    js_click = f"""
        // Use standard querySelector with the simplified selector
        var submitButton = document.querySelector('{SUBMIT_BUTTON_SELECTOR}');
        if (submitButton && !submitButton.disabled) {{
            submitButton.click();
            console.log('Submit button clicked using selector: {SUBMIT_BUTTON_SELECTOR}');
            true; // Indicate success
        }} else {{
            if (!submitButton) {{
                 console.error('Could not find submit button: {SUBMIT_BUTTON_SELECTOR}');
            }} else if (submitButton.disabled) {{
                 console.error('Submit button is disabled.');
            }}
            false; // Indicate failure
        }}
    """
    await send_cdp_command(ws, "Runtime.evaluate", {"expression": js_click, "awaitPromise": False}, session_id=session_id)
    print("Submit action triggered. Waiting for WebSocket messages...")
    await asyncio.sleep(3) # Wait for message sending process

    # WebSocket message monitoring loop
    print(f"Monitoring WebSocket frames for {WEBSOCKET_URL_FILTER} (Press Ctrl+C to stop)...")
    target_websocket_request_id = None # Store the requestId of the target WebSocket
    try:
        while True:
            message = await ws.recv()
            data = json.loads(message)

            # Check if the event is related to this session
            if data.get("sessionId") == session_id and "method" in data:
                method = data["method"]
                params = data.get("params", {})

                # WebSocket connection established event
                if method == "Network.webSocketCreated":
                    # --- Example WebSocket Message Flow (Observed during testing) ---
                    # Note: conversationId, messageId, timestamps, etc., will vary.
                    #
                    # [Sent] Client initiates options
                    # Payload: {"event":"setOptions","supportedFeatures":[],"supportedCards":["weather",...],"ads":null}
                    #
                    # [Sent] Client sends the user message
                    # Payload: {"event":"send","conversationId":"dRUXwuM545tff65JPZ3rn","content":[{"type":"text","text":"This is an automated test message."}],"mode":"chat","context":{}}
                    #
                    # [Received] Server acknowledges receipt
                    # Payload: {"event":"received","conversationId":"dRUXwuM545tff65JPZ3rn","messageId":"17Wzuonj3Q54L3oYsbPdC","createdAt":"...","id":"0"}
                    #
                    # [Received] Server starts generating response
                    # Payload: {"event":"startMessage","conversationId":"dRUXwuM545tff65JPZ3rn","messageId":"cunqiwyXHxKALffniSEnf","createdAt":"...","id":"1"}
                    #
                    # [Received] Server sends response text chunk by chunk
                    # Payload: {"event":"appendText","messageId":"cunqiwyXHxKALffniSEnf","partId":"0","text":"Received","id":"2"}
                    # Payload: {"event":"appendText","messageId":"cunqiwyXHxKALffniSEnf","partId":"0","text":"! Test","id":"3"}
                    # ... (more appendText messages) ...
                    # Payload: {"event":"appendText","messageId":"cunqiwyXHxKALffniSEnf","partId":"0","text":"Feel free.\n","id":"7"}
                    #
                    # [Received] Server indicates a part is complete
                    # Payload: {"event":"partCompleted","messageId":"cunqiwyXHxKALffniSEnf","partId":"0","id":"8"}
                    #
                    # [Received] Server indicates the end of the response stream
                    # Payload: {"event":"done","messageId":"cunqiwyXHxKALffniSEnf","id":"9"}
                    #
                    # [Received] Server updates chat title (optional)
                    # Payload: {"event":"titleUpdate","conversationId":"dRUXwuM545tff65JPZ3rn","title":"Test message receipt confirmation","id":"10"}
                    #
                    # [Received] Server provides follow-up suggestions (optional)
                    # Payload: {"event":"suggestedFollowups","messageId":"cunqiwyXHxKALffniSEnf","suggestions":["Can you show me what you can do?",...],"id":"11"}
                    # ---------------------------------------------------------------------
                    if params.get("url") == WEBSOCKET_URL_FILTER:
                        target_websocket_request_id = params.get("requestId")
                        print(f"\n+++ Target WebSocket created. Request ID: {target_websocket_request_id} +++\n")

                # WebSocket frame sent event (filter by requestId)
                elif method == "Network.webSocketFrameSent":
                    if params.get("requestId") == target_websocket_request_id:
                        try:
                            payload_str = params.get('response', {}).get('payloadData', '{}')
                            payload_json = json.loads(payload_str)
                            event_type = payload_json.get("event")
                            # Print simplified event type for sent messages
                            print(f"\n[Sent Event: {event_type}]")
                        except json.JSONDecodeError:
                            print("\n[Sent Event: (Non-JSON Payload)]")
                        except Exception as e:
                             print(f"\n[Error processing Sent Event: {e}]")


                # WebSocket frame received event (filter by requestId & extract appendText)
                elif method == "Network.webSocketFrameReceived":
                    if params.get("requestId") == target_websocket_request_id:
                        try:
                            payload_str = params.get('response', {}).get('payloadData', '{}')
                            payload_json = json.loads(payload_str)
                            event_type = payload_json.get("event")

                            if event_type == "appendText":
                                response_text = payload_json.get("text", "")
                                # Yield response chunk directly
                                yield response_text
                            elif event_type: # Print other known event types for context, add newline before
                                print(f"\n[Received Event: {event_type}]", flush=True)
                                if event_type == "done":
                                    print("Response complete (received 'done' event).")
                                    return # Stop monitoring and return to REPL
                            # else: # Optional: Handle cases with no event type if needed
                            #     print(f"\n[Received Payload: {payload_str}]")

                        except json.JSONDecodeError:
                            # Handle cases where payload is not valid JSON (e.g., binary frames if any)
                            print("\n[Received Event: (Non-JSON Payload)]", flush=True)
                        except Exception as e:
                            print(f"\n[Error processing Received Event: {e}]", flush=True)


                # Other events (for debugging - uncomment if needed)
                # elif not method.startswith("Network.webSocket"):
                #    print(f"Other CDP Event: {method}")

    except websockets.exceptions.ConnectionClosedOK:
        print("Browser WebSocket connection closed.")
    except asyncio.CancelledError:
        print("Task cancelled.")
    except KeyboardInterrupt:
        print("Stopping listener...")
    except Exception as e:
        print(f"Error during WebSocket monitoring: {e}")


async def main():
    if platform.system() != "Windows":
        print("This script is designed for Windows.")
        return

    # Create debug profile directory if specified and doesn't exist
    if DEBUG_PROFILE_DIR and not os.path.exists(DEBUG_PROFILE_DIR):
        try:
            os.makedirs(DEBUG_PROFILE_DIR)
            print(f"Created temporary profile directory: {DEBUG_PROFILE_DIR}")
        except OSError as e:
            print(f"Warning: Could not create profile directory {DEBUG_PROFILE_DIR}: {e}")
            # Decide if this is fatal or if we should proceed without a profile dir
            # For now, let's proceed, Edge might handle it or fail later.
    
    # Start Edge in debug mode
    print(f"Starting Edge with debugging on port {DEBUGGING_PORT}...")
    edge_process = None
    try:
        edge_args = [
            EDGE_PATH,
            f"--remote-debugging-port={DEBUGGING_PORT}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-restore-session-state", # Prevent session restore prompt
            "--restore-last-session=false", # Add another flag to prevent restore
            "--auto-open-devtools-for-tabs", # Open DevTools automatically
            "--disable-session-crashed-bubble" # Disable the "Restore pages" bubble
            # COPILOT_URL # Opening URL on startup is possible, but controlling via CDP is more reliable
        ]
        # Conditionally add the user data directory argument
        if DEBUG_PROFILE_DIR:
            edge_args.append(f"--user-data-dir={DEBUG_PROFILE_DIR}")
        # Use subprocess.list2cmdline for safer argument joining on Windows if needed for printing,
        # but passing the list directly to Popen is generally safer.
        print(f"Starting Edge with args: (see list)") # Avoid potential quoting issues in print
        edge_process = subprocess.Popen(edge_args)
        print(f"Edge process started (PID: {edge_process.pid}). Waiting for browser to initialize (5s)...")
        await asyncio.sleep(5) # Reduced initial wait time
    except FileNotFoundError:
        print(f"Error: Edge executable not found at {EDGE_PATH}")
        print("Please check the EDGE_PATH variable in the script.")
        return
    except Exception as e:
        print(f"Error starting Edge: {e}")
        return

    # --- Dynamically get the browser debugger WebSocket URL ---
    websocket_connection = None
    session_id = None
    browser_ws_url = None
    max_fetch_retries = 10
    fetch_retry_delay = 2 # seconds
    version_url = f"http://127.0.0.1:{DEBUGGING_PORT}/json/version"

    print(f"Attempting to fetch debugger URL from {version_url}...")
    for attempt in range(max_fetch_retries):
        try:
            with urllib.request.urlopen(version_url, timeout=5) as response:
                if response.status == 200:
                    version_data = json.loads(response.read().decode())
                    browser_ws_url = version_data.get("webSocketDebuggerUrl")
                    if browser_ws_url:
                        print(f"Successfully fetched debugger URL: {browser_ws_url}")
                        break # Success
                    else:
                        print("webSocketDebuggerUrl not found in version info.")
                else:
                    print(f"Failed to fetch version info (HTTP {response.status}).")

        except urllib.error.URLError as e:
            print(f"Error fetching version info: {e}. Is the browser running and debugging enabled?")
        except Exception as e:
            print(f"Unexpected error fetching version info: {e}")

        if browser_ws_url:
            break # Exit loop if URL was fetched

        if attempt < max_fetch_retries - 1:
            print(f"Retrying fetch in {fetch_retry_delay} seconds... ({attempt + 1}/{max_fetch_retries})")
            await asyncio.sleep(fetch_retry_delay)
        else:
            print("Max retries reached. Could not fetch debugger URL.")
            # Terminate Edge process if fetch fails
            if edge_process:
                print("Terminating Edge process due to fetch failure.")
                edge_process.terminate()
                try: edge_process.wait(timeout=5)
                except subprocess.TimeoutExpired: edge_process.kill()
            return # Exit script

    if not browser_ws_url:
         print("Exiting script as debugger URL could not be obtained.")
         return

    # --- Connect using the fetched WebSocket URL ---
    try:
        print(f"Connecting to browser debugger WebSocket: {browser_ws_url}")
        websocket_connection = await websockets.connect(browser_ws_url, ping_interval=None, ping_timeout=None)
        print("Connected to browser debugger WebSocket.")

        # Find page target
        target_id = await find_page_target(websocket_connection)
        if not target_id:
            print("Could not find target page. Exiting.")
            return # Process termination in finally block

        # Attach to target and get session ID
        session_id = await attach_to_target(websocket_connection, target_id)
        if not session_id:
            print("Failed to attach to target page. Exiting.")
            return # Process termination in finally block

        # --- REPL for interacting with Copilot ---
        print("\nCopilot REPL initialized. Type your message and press Enter.")
        print("Type 'exit' or 'quit' or press Ctrl+D (EOF) to terminate.")
        while True:
            try:
                sys.stdout.write("> ")
                sys.stdout.flush()
                user_input = sys.stdin.readline().strip()

                if not user_input or user_input.lower() in ["exit", "quit"]:
                    print("\nExiting REPL...")
                    break

                print(f"Sending to Copilot: {user_input}")
                async for response_chunk in monitor_copilot_interaction(websocket_connection, session_id, user_input):
                    sys.stdout.write(response_chunk)
                    sys.stdout.flush()
                sys.stdout.write("\n") # Add a newline after the full response
                sys.stdout.flush()

            except EOFError:
                print("\nEOF received, exiting REPL...")
                break
            except KeyboardInterrupt:
                print("\nREPL interrupted by user. Type 'exit' or 'quit' to close.")
                # Allow continuing the REPL or exiting cleanly
                continue # Or break, depending on desired behavior
            except Exception as e_repl:
                print(f"\nError in REPL loop: {e_repl}")
                # Potentially break or offer to retry depending on the error
                break # For now, exit on other errors

    except ConnectionRefusedError:
        print(f"Connection refused. Is Edge running with --remote-debugging-port={DEBUGGING_PORT}?")
        print("Ensure no other process is using the port and Edge started correctly.")
    except websockets.exceptions.InvalidURI:
         print(f"Invalid WebSocket URI: {browser_ws_url}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        import traceback
        traceback.print_exc() # Detailed error display
    finally:
        if websocket_connection: # Check if connection object exists before trying to close
            try:
                await websocket_connection.close()
                print("Browser WebSocket connection closed.")
            except Exception as e:
                # Log potential errors during close, but don't crash
                print(f"Error closing WebSocket connection: {e}")
        if edge_process:
            print("Terminating Edge process...")
            edge_process.terminate()
            try:
                edge_process.wait(timeout=5) # Wait for termination
            except subprocess.TimeoutExpired:
                print("Edge did not terminate gracefully, killing.")
                edge_process.kill()
            print("Edge process terminated.")
        print("Script finished.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nScript interrupted by user.")
