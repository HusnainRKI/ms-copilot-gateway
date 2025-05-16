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
import typing # Import typing module

# --- CDP Related ---
_cdp_message_id_counter = 0

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
        self.target_id: typing.Optional[str] = None
        self.is_page_initialized: bool = False
        self.chat_websocket_request_id: typing.Optional[str] = None
        self.is_first_message_sent: bool = False

    async def _send_cdp_command(self, ws, method, params={}, session_id=None):
        self._cdp_message_id += 1
        msg = {"id": self._cdp_message_id, "method": method, "params": params}
        if session_id: msg["sessionId"] = session_id
        await ws.send(json.dumps(msg))
        return self._cdp_message_id

    async def _find_page_target(self, ws):
        list_targets_id = await self._send_cdp_command(ws, "Target.getTargets")
        while True:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=5.0)
                data = json.loads(message)
                if data.get("id") == list_targets_id and "result" in data:
                    for target in data["result"]["targetInfos"]:
                        if target.get("type") == "page" and not target.get("url", "").startswith("devtools://"):
                            print(f"Found target: {target['targetId']} - {target.get('url')}")
                            return target["targetId"]
                    return None
                elif "error" in data and data.get("id") == list_targets_id: return None
            except asyncio.TimeoutError: return None
            except Exception: return None

    async def _attach_to_target(self, ws, target_id):
        attach_cmd_id = await self._send_cdp_command(ws, "Target.attachToTarget", {"targetId": target_id, "flatten": True})
        while True:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=10.0)
                data = json.loads(message)
                if data.get("id") == attach_cmd_id and "result" in data:
                    return data["result"]["sessionId"]
                elif data.get("method") == "Target.attachedToTarget" and \
                     data.get("params", {}).get("targetInfo", {}).get("targetId") == target_id:
                    return data.get("params", {}).get("sessionId")
                elif "error" in data and data.get("id") == attach_cmd_id: return None
            except asyncio.TimeoutError: return None
            except Exception: return None

    async def connect(self):
        if self.websocket_connection and self.session_id and self.is_page_initialized:
            return True
        if platform.system() != "Windows": raise RuntimeError("This script is designed for Windows only.")
        if self.debug_profile_dir and not os.path.exists(self.debug_profile_dir):
            try: os.makedirs(self.debug_profile_dir)
            except OSError as e: print(f"Warning: Could not create profile directory {self.debug_profile_dir}: {e}")

        try:
            edge_args = [self.edge_path, f"--remote-debugging-port={self.debugging_port}", "--no-first-run",
                         "--no-default-browser-check", "--no-restore-session-state", "--restore-last-session=false",
                         "--disable-session-crashed-bubble"]
            if self.debug_profile_dir: edge_args.append(f"--user-data-dir={self.debug_profile_dir}")
            self.edge_process = subprocess.Popen(edge_args)
            await asyncio.sleep(5)
        except FileNotFoundError: raise RuntimeError(f"Edge executable not found at {self.edge_path}")
        except Exception as e: raise RuntimeError(f"Error starting Edge: {str(e)}")

        version_url = f"http://127.0.0.1:{self.debugging_port}/json/version"
        for attempt in range(10):
            try:
                with urllib.request.urlopen(version_url, timeout=5) as response:
                    if response.status == 200:
                        self.browser_ws_url = json.loads(response.read().decode()).get("webSocketDebuggerUrl")
                        if self.browser_ws_url: break
            except Exception: pass
            if attempt == 9: raise RuntimeError("Could not fetch debugger URL for Edge.")
            await asyncio.sleep(2)

        try:
            if not self.browser_ws_url: # Added from previous Pylance fix
                raise RuntimeError("Browser WebSocket URL is None before attempting to connect.")
            self.websocket_connection = await websockets.connect(self.browser_ws_url, ping_interval=None, ping_timeout=None)
            self.target_id = await self._find_page_target(self.websocket_connection)
            if not self.target_id: raise RuntimeError("Could not find a suitable page target.")
            self.session_id = await self._attach_to_target(self.websocket_connection, self.target_id)
            if not self.session_id: raise RuntimeError("Failed to attach to the page target.")

            if not self.is_page_initialized:
                await self._send_cdp_command(self.websocket_connection, "Page.navigate", {"url": self.copilot_url}, session_id=self.session_id)
                page_load_timeout = 3.0 # Reduced timeout
                load_event_fired = False; start_load_wait = time.time()
                while time.time() - start_load_wait < page_load_timeout:
                    try:
                        message_str = await asyncio.wait_for(self.websocket_connection.recv(), timeout=1.0)
                        event_data = json.loads(message_str)
                        if event_data.get("method") == "Page.loadEventFired" and event_data.get("sessionId") == self.session_id:
                            load_event_fired = True; break
                    except asyncio.TimeoutError: pass
                    except Exception: break
                if not load_event_fired: print(f"Warning: Page.loadEventFired not received in {page_load_timeout}s.")
                await asyncio.sleep(2)
                for domain in ["Page", "Network", "Runtime", "DOM"]:
                    await self._send_cdp_command(self.websocket_connection, f"{domain}.enable", {}, session_id=self.session_id)
                self.is_page_initialized = True; self.is_first_message_sent = False
            return True
        except Exception as e: await self.close(); raise RuntimeError(f"Error in connect setup: {str(e)}")

    async def _capture_chat_websocket_id(self, ws, session_id):
        print("Starting WebSocket ID capture task...")
        timeout = 18; start_time = time.time()
        try:
            while time.time() - start_time < timeout:
                try:
                    message_str = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(message_str)
                    if data.get("sessionId") == session_id and data.get("method") == "Network.webSocketCreated":
                        params = data.get("params", {})
                        if params.get("url") and params.get("url").startswith(self.websocket_url_filter):
                            self.chat_websocket_request_id = params.get("requestId")
                            print(f"Captured chat WebSocket ID (in task): {self.chat_websocket_request_id}")
                            return
                except asyncio.TimeoutError: pass
                except json.JSONDecodeError: print("Warning (capture task): Non-JSON message.")
                except Exception as inner_e: print(f"Inner exception in capture task: {inner_e}"); await asyncio.sleep(0.1)
            print(f"Capture task: WebSocket ID not found in {timeout}s.")
        except asyncio.CancelledError: print("WebSocket ID capture task cancelled."); raise
        except Exception as e: print(f"Exception in _capture_chat_websocket_id: {e}")


    async def send_message_and_get_response(self, user_message: str):
        if not self.is_page_initialized or not self.websocket_connection or not self.session_id:
            raise RuntimeError("Copilot client not connected or page not initialized.")

        ws = self.websocket_connection; session_id = self.session_id
        # The self.chat_websocket_request_id is crucial for identifying the WebSocket connection
        # used for the actual chat messages with Copilot.
        # This ID is obtained when the first message is sent and the corresponding
        # "Network.webSocketCreated" event is captured.
        # For subsequent messages within the same Copilot session (i.e., same browser tab),
        # this ID remains the same and must be reused.
        # Therefore, it's captured only once if it's None and then reused.

        print(f"Simulating typing: '{user_message}'")
        doc_root_id_cmd = await self._send_cdp_command(ws, "DOM.getDocument", {"depth": -1}, session_id=session_id)
        root_node_id = None
        try:
            while True:
                message_str = await asyncio.wait_for(ws.recv(), timeout=10.0)
                data = json.loads(message_str)
                if data.get("id") == doc_root_id_cmd:
                    if "result" in data and data["result"].get("root"): root_node_id = data["result"]["root"]["nodeId"]
                    elif "error" in data: raise RuntimeError(f"CDP Error (DOM.getDocument): {data['error'].get('message')}")
                    break
        except Exception as e: raise RuntimeError(f"Error getting document root: {str(e)}")
        if not root_node_id: raise RuntimeError("Failed to obtain document root node ID.")

        query_selector_id_cmd = await self._send_cdp_command(ws, "DOM.querySelector", {"nodeId": root_node_id, "selector": self.user_input_selector}, session_id=session_id)
        textarea_node_id = None
        try:
            while True:
                message_str = await asyncio.wait_for(ws.recv(), timeout=10.0)
                data = json.loads(message_str)
                if data.get("id") == query_selector_id_cmd:
                    if "result" in data and data["result"].get("nodeId") is not None:
                        textarea_node_id = data["result"]["nodeId"]
                        if textarea_node_id == 0: raise RuntimeError(f"Textarea '{self.user_input_selector}' not found.")
                    elif "error" in data: raise RuntimeError(f"CDP Error (DOM.querySelector): {data['error'].get('message')}")
                    break
        except Exception as e: raise RuntimeError(f"Error querying selector '{self.user_input_selector}': {str(e)}")
        if not textarea_node_id: raise RuntimeError(f"Failed to obtain textarea node ID for '{self.user_input_selector}'.")
        
        await self._send_cdp_command(ws, "DOM.focus", {"nodeId": textarea_node_id}, session_id=session_id)
        await asyncio.sleep(0.5)
        await self._send_cdp_command(ws, "Input.insertText", {"text": user_message}, session_id=session_id)
        print("Text insertion command sent.")
        await asyncio.sleep(0.5)

        capture_task = None
        if self.chat_websocket_request_id is None:
            # This is typically for the first message in a session.
            # The chat WebSocket is usually created after the first user interaction (message send).
            # We start a task to listen for the "Network.webSocketCreated" event.
            print("chat_websocket_request_id is None. Starting WebSocket ID capture task...")
            capture_task = asyncio.create_task(self._capture_chat_websocket_id(ws, session_id))
        else:
            print(f"Reusing existing chat_websocket_request_id: {self.chat_websocket_request_id}")

        print("Clicking submit button...")
        js_click = f"document.querySelector('{self.submit_button_selector}')?.click();"
        await self._send_cdp_command(ws, "Runtime.evaluate", {"expression": js_click, "awaitPromise": False}, session_id=session_id)
        # Assuming click is successful, proceed. Error handling for click can be added if needed.
        await asyncio.sleep(0.2) 

        if capture_task: # Only wait for capture_task if it was created
            print("Waiting for WebSocket ID capture task to complete...")
            try:
                await asyncio.wait_for(capture_task, timeout=20.0) 
                if self.chat_websocket_request_id is None and not capture_task.cancelled():
                    raise RuntimeError("WebSocket ID capture task completed but ID is still None.")
            except asyncio.TimeoutError:
                print("Timeout waiting for WebSocket ID capture task to complete.")
                if not capture_task.done(): capture_task.cancel()
                if self.chat_websocket_request_id is None: 
                    raise RuntimeError("Timeout waiting for chat WebSocket Request ID capture task.")
            except asyncio.CancelledError:
                print("WebSocket ID capture task was cancelled.")
                if self.chat_websocket_request_id is None: 
                    raise RuntimeError("Chat WebSocket Request ID capture was cancelled before completion.")
            except Exception as e_capture_wait:
                print(f"Error waiting for WebSocket ID capture task: {e_capture_wait}")
                if not capture_task.done(): capture_task.cancel()
                if self.chat_websocket_request_id is None:
                    raise RuntimeError(f"Error during WebSocket ID capture: {str(e_capture_wait)}")
        
        if not self.chat_websocket_request_id:
            raise RuntimeError("Failed to obtain chat_websocket_request_id for monitoring.")

        target_websocket_request_id = self.chat_websocket_request_id
        print(f"Monitoring WebSocket frames for Request ID: {target_websocket_request_id}...")
        try:
            while True:
                message_str = await ws.recv()
                data = json.loads(message_str)
                if data.get("sessionId") == session_id and "method" in data and \
                   data.get("params", {}).get("requestId") == target_websocket_request_id:
                    if data["method"] == "Network.webSocketFrameReceived":
                        try:
                            payload_json = json.loads(data["params"]['response']['payloadData'])
                            event_type = payload_json.get("event")
                            if event_type == "appendText": yield payload_json.get("text", "")
                            elif event_type == "done": print("Response complete ('done' event)."); return
                            elif event_type: print(f"[Received Event: {event_type}]", flush=True)
                        except Exception as e_recv: print(f"[Error processing Received Frame: {e_recv}]", flush=True)
        except websockets.exceptions.ConnectionClosed: print("Browser WebSocket closed (monitoring).")
        except Exception as e_mon: raise RuntimeError(f"Error during WebSocket monitoring: {str(e_mon)}")
        finally:
            if not self.is_first_message_sent: self.is_first_message_sent = True

    async def close(self):
        if self.websocket_connection:
            try: await self.websocket_connection.close()
            except Exception: pass 
            self.websocket_connection = None
        if self.edge_process:
            try: self.edge_process.terminate(); self.edge_process.wait(timeout=2)
            except Exception: self.edge_process.kill()
            self.edge_process = None
        self.session_id = self.browser_ws_url = self.target_id = self.chat_websocket_request_id = None
        self.is_page_initialized = self.is_first_message_sent = False
        print("Copilot client session attributes reset and Edge process handled.")