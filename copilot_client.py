import asyncio
import json
import subprocess
import websockets
import time
import platform
import os
import tempfile
import urllib.request
import urllib.error

# --- CDP Related ---
_cdp_message_id_counter = 0 # Renamed to avoid conflict if used as class var directly

class CopilotClient:
    def __init__(self, edge_path: str, debug_profile_dir: str | None, debugging_port: int,
                 copilot_url: str, websocket_url_filter: str,
                 user_input_selector: str, submit_button_selector: str):
        self.edge_path = edge_path
        self.debug_profile_dir = debug_profile_dir
        self.debugging_port = debugging_port
        self.copilot_url = copilot_url
        self.websocket_url_filter = websocket_url_filter
        self.user_input_selector = user_input_selector
        self.submit_button_selector = submit_button_selector

        self._cdp_message_id = 0
        self.edge_process = None
        self.websocket_connection = None
        self.session_id = None
        self.browser_ws_url = None

    async def _send_cdp_command(self, ws, method, params={}, session_id=None):
        """Send a CDP command and return the message ID"""
        self._cdp_message_id += 1
        msg = {"id": self._cdp_message_id, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        # print(f"Sending CDP: {msg}") # For debugging
        await ws.send(json.dumps(msg))
        return self._cdp_message_id

    async def _find_page_target(self, ws):
        """Find the first available page target ID"""
        list_targets_id = await self._send_cdp_command(ws, "Target.getTargets")
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

    async def _attach_to_target(self, ws, target_id):
        """Attach to the specified target ID and return the session ID"""
        attach_cmd_id = await self._send_cdp_command(ws, "Target.attachToTarget", {"targetId": target_id, "flatten": True})
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

    async def connect(self):
        """Starts Edge, connects to debugger, and attaches to Copilot page."""
        if platform.system() != "Windows":
            print("This script is designed for Windows.")
            # Consider raising an exception here for clarity
            return False

        # Create debug profile directory if specified and doesn't exist
        if self.debug_profile_dir and not os.path.exists(self.debug_profile_dir):
            try:
                os.makedirs(self.debug_profile_dir)
                print(f"Created temporary profile directory: {self.debug_profile_dir}")
            except OSError as e:
                print(f"Warning: Could not create profile directory {self.debug_profile_dir}: {e}")
                # Decide if this is fatal or if we should proceed without a profile dir
                # For now, let's proceed, Edge might handle it or fail later.

        # Start Edge in debug mode
        print(f"Starting Edge with debugging on port {self.debugging_port}...")
        try:
            edge_args = [
                self.edge_path,
                f"--remote-debugging-port={self.debugging_port}",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-restore-session-state", # Prevent session restore prompt
                "--restore-last-session=false", # Add another flag to prevent restore
                "--auto-open-devtools-for-tabs", # Open DevTools automatically
                "--disable-session-crashed-bubble", # Disable the "Restore pages" bubble
                # self.copilot_url # Opening URL on startup is possible, but controlling via CDP is more reliable
            ]
            if self.debug_profile_dir:
                edge_args.append(f"--user-data-dir={self.debug_profile_dir}")
            print(f"Starting Edge with args: (see list)")
            self.edge_process = subprocess.Popen(edge_args)
            print(f"Edge process started (PID: {self.edge_process.pid}). Waiting for browser to initialize (5s)...")
            await asyncio.sleep(5)
        except FileNotFoundError:
            print(f"Error: Edge executable not found at {self.edge_path}")
            print("Please check the EDGE_PATH variable in the script.")
            return False
        except Exception as e:
            print(f"Error starting Edge: {e}")
            return False

        # Dynamically get the browser debugger WebSocket URL
        max_fetch_retries = 10
        fetch_retry_delay = 2 # seconds
        version_url = f"http://127.0.0.1:{self.debugging_port}/json/version"

        print(f"Attempting to fetch debugger URL from {version_url}...")
        for attempt in range(max_fetch_retries):
            try:
                with urllib.request.urlopen(version_url, timeout=5) as response:
                    if response.status == 200:
                        version_data = json.loads(response.read().decode())
                        self.browser_ws_url = version_data.get("webSocketDebuggerUrl")
                        if self.browser_ws_url:
                            print(f"Successfully fetched debugger URL: {self.browser_ws_url}")
                            break
                        else:
                            print("webSocketDebuggerUrl not found in version info.")
                    else:
                        print(f"Failed to fetch version info (HTTP {response.status}).")
            except urllib.error.URLError as e:
                print(f"Error fetching version info: {e}. Is the browser running and debugging enabled?")
            except Exception as e:
                print(f"Unexpected error fetching version info: {e}")

            if self.browser_ws_url:
                break

            if attempt < max_fetch_retries - 1:
                print(f"Retrying fetch in {fetch_retry_delay} seconds... ({attempt + 1}/{max_fetch_retries})")
                await asyncio.sleep(fetch_retry_delay)
            else:
                print("Max retries reached. Could not fetch debugger URL.")
                await self.close() # Ensure Edge is closed if fetch fails
                return False

        if not self.browser_ws_url:
             print("Exiting script as debugger URL could not be obtained.")
             return False

        # Connect using the fetched WebSocket URL
        try:
            print(f"Connecting to browser debugger WebSocket: {self.browser_ws_url}")
            self.websocket_connection = await websockets.connect(self.browser_ws_url, ping_interval=None, ping_timeout=None)
            print("Connected to browser debugger WebSocket.")

            target_id = await self._find_page_target(self.websocket_connection)
            if not target_id:
                print("Could not find target page. Exiting.")
                await self.close()
                return False

            self.session_id = await self._attach_to_target(self.websocket_connection, target_id)
            if not self.session_id:
                print("Failed to attach to target page. Exiting.")
                await self.close()
                return False
            return True
        except ConnectionRefusedError:
            print(f"Connection refused. Is Edge running with --remote-debugging-port={self.debugging_port}?")
            print("Ensure no other process is using the port and Edge started correctly.")
            await self.close()
            return False
        except websockets.exceptions.InvalidURI:
             print(f"Invalid WebSocket URI: {self.browser_ws_url}")
             await self.close()
             return False
        except Exception as e:
            print(f"An unexpected error occurred during connect: {e}")
            import traceback
            traceback.print_exc()
            await self.close()
            return False


    async def send_message_and_get_response(self, user_message: str):
        """Interact with the Copilot page and monitor WebSocket communication, yielding responses."""
        if not self.websocket_connection or not self.session_id:
            print("Not connected to Copilot. Please call connect() first.")
            # Consider raising an exception or returning an empty generator
            return

        ws = self.websocket_connection # Alias for convenience
        session_id = self.session_id

        # Enable necessary CDP domains
        await self._send_cdp_command(ws, "Page.enable", {}, session_id=session_id)
        await self._send_cdp_command(ws, "Network.enable", {}, session_id=session_id)
        await self._send_cdp_command(ws, "Runtime.enable", {}, session_id=session_id)
        await self._send_cdp_command(ws, "DOM.enable", {}, session_id=session_id) # Enable DOM domain
        print("CDP Domains enabled (Page, Network, Runtime, DOM).")

        # Open Copilot
        print(f"Navigating to {self.copilot_url}...")
        await self._send_cdp_command(ws, "Page.navigate", {"url": self.copilot_url}, session_id=session_id)

        # Wait for page load (shortened)
        print("Waiting for page load (3s)...")
        await asyncio.sleep(3) # Reduced wait time

        # --- Simulate typing the user message using CDP Input domain ---
        print(f"Simulating typing: '{user_message}'")
        # 1. Get document root node ID
        doc_root_id_cmd = await self._send_cdp_command(ws, "DOM.getDocument", {"depth": -1}, session_id=session_id)
        root_node_id = None
        while root_node_id is None:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=5.0)
                data = json.loads(message)
                if data.get("id") == doc_root_id_cmd:
                    if "result" in data:
                        root_node_id = data["result"]["root"]["nodeId"]
                        print("Got document root node ID.")
                    elif "error" in data:
                        print(f"Error getting document root: {data['error']}")
                    break
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
            print(f"Querying for selector: {self.user_input_selector}")
            query_selector_id_cmd = await self._send_cdp_command(ws, "DOM.querySelector", {"nodeId": root_node_id, "selector": self.user_input_selector}, session_id=session_id)
            textarea_node_id = None
            while textarea_node_id is None:
                 try:
                     message = await asyncio.wait_for(ws.recv(), timeout=5.0)
                     data = json.loads(message)
                     if data.get("id") == query_selector_id_cmd:
                         if "result" in data:
                             textarea_node_id = data["result"]["nodeId"]
                             if textarea_node_id == 0:
                                 print(f"Could not find textarea element with selector: {self.user_input_selector}")
                                 textarea_node_id = None
                             else:
                                 print("Found textarea node ID.")
                         elif "error" in data:
                             print(f"Error querying selector '{self.user_input_selector}': {data['error']}")
                         break
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
                await self._send_cdp_command(ws, "DOM.focus", {"nodeId": textarea_node_id}, session_id=session_id)
                await asyncio.sleep(0.5)

                # 4. Insert text using Input.insertText
                print("Inserting text...")
                await self._send_cdp_command(ws, "Input.insertText", {"text": user_message}, session_id=session_id)
                print("Text insertion command sent.")
                await asyncio.sleep(1)

        # --- Click the submit button (Shadow DOM handling) ---
        print("Clicking submit button...")
        js_click = f"""
            var submitButton = document.querySelector('{self.submit_button_selector}');
            if (submitButton && !submitButton.disabled) {{
                submitButton.click();
                console.log('Submit button clicked using selector: {self.submit_button_selector}');
                true;
            }} else {{
                if (!submitButton) {{
                     console.error('Could not find submit button: {self.submit_button_selector}');
                }} else if (submitButton.disabled) {{
                     console.error('Submit button is disabled.');
                }}
                false;
            }}
        """
        await self._send_cdp_command(ws, "Runtime.evaluate", {"expression": js_click, "awaitPromise": False}, session_id=session_id)
        print("Submit action triggered. Waiting for WebSocket messages...")
        await asyncio.sleep(3)

        # WebSocket message monitoring loop
        print(f"Monitoring WebSocket frames for {self.websocket_url_filter} (Press Ctrl+C to stop)...")
        target_websocket_request_id = None
        try:
            while True:
                message = await ws.recv()
                data = json.loads(message)

                if data.get("sessionId") == session_id and "method" in data:
                    method = data["method"]
                    params = data.get("params", {})

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
                        if params.get("url") and params.get("url").startswith(self.websocket_url_filter):
                            target_websocket_request_id = params.get("requestId")
                            print(f"\n+++ Target WebSocket created. Request ID: {target_websocket_request_id} +++\n")

                    elif method == "Network.webSocketFrameSent":
                        if params.get("requestId") == target_websocket_request_id:
                            try:
                                payload_str = params.get('response', {}).get('payloadData', '{}')
                                payload_json = json.loads(payload_str)
                                event_type = payload_json.get("event")
                                print(f"\n[Sent Event: {event_type}]")
                            except json.JSONDecodeError:
                                print("\n[Sent Event: (Non-JSON Payload)]")
                            except Exception as e:
                                 print(f"\n[Error processing Sent Event: {e}]")

                    elif method == "Network.webSocketFrameReceived":
                        if params.get("requestId") == target_websocket_request_id:
                            try:
                                payload_str = params.get('response', {}).get('payloadData', '{}')
                                payload_json = json.loads(payload_str)
                                event_type = payload_json.get("event")

                                if event_type == "appendText":
                                    response_text = payload_json.get("text", "")
                                    yield response_text
                                elif event_type:
                                    print(f"\n[Received Event: {event_type}]", flush=True)
                                    if event_type == "done":
                                        print("Response complete (received 'done' event).")
                                        return
                            except json.JSONDecodeError:
                                print("\n[Received Event: (Non-JSON Payload)]", flush=True)
                            except Exception as e:
                                print(f"\n[Error processing Received Event: {e}]", flush=True)

        except websockets.exceptions.ConnectionClosedOK:
            print("Browser WebSocket connection closed (during monitoring).")
        except asyncio.CancelledError:
            print("Task cancelled (during monitoring).")
        # KeyboardInterrupt should be handled by the caller (main loop)
        except Exception as e:
            print(f"Error during WebSocket monitoring: {e}")


    async def close(self):
        """Closes WebSocket connection and terminates Edge process."""
        if self.websocket_connection:
            try:
                await self.websocket_connection.close()
                print("Browser WebSocket connection closed.")
            except Exception as e:
                print(f"Error closing WebSocket connection: {e}")
            finally:
                self.websocket_connection = None

        if self.edge_process:
            print("Terminating Edge process...")
            try:
                self.edge_process.terminate()
                # Wait for a short period to allow graceful termination
                try:
                    self.edge_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print("Edge process did not terminate gracefully, killing.")
                    self.edge_process.kill()
                print("Edge process terminated.")
            except Exception as e:
                print(f"Error terminating Edge process: {e}")
            finally:
                self.edge_process = None
        self.session_id = None
        self.browser_ws_url = None