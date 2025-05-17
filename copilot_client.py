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
import logging # Added for logging

# --- Logger Setup ---
logger = logging.getLogger("CopilotClient")

class CopilotClient:
    def __init__(self, edge_path: str, debug_profile_dir: str | None, debugging_port: int,
                 copilot_url: str, websocket_url_filter: str,
                 user_input_selector: str, submit_button_selector: str,
                 is_debug_logging: bool = False): # Added is_debug_logging
        self.edge_path = edge_path
        self.debug_profile_dir = debug_profile_dir
        self.debugging_port = debugging_port
        self.copilot_url = copilot_url
        self.websocket_url_filter = websocket_url_filter
        self.user_input_selector = user_input_selector
        self.submit_button_selector = submit_button_selector
        self.is_debug_logging = is_debug_logging # Store the flag

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
        if self.is_debug_logging: # Log detailed CDP commands only in debug mode
            logger.debug(f"Sending CDP command: ID={self._cdp_message_id}, Method={method}, Params={json.dumps(params)}, SessionID={session_id}")
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
                            logger.info(f"Found target: {target['targetId']} - {target.get('url')}")
                            return target["targetId"]
                    logger.warning("No suitable page target found in Target.getTargets response.")
                    return None
                elif "error" in data and data.get("id") == list_targets_id:
                    logger.error(f"Error in Target.getTargets response: {data['error']}")
                    return None
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for Target.getTargets response.")
                return None
            except Exception as e:
                logger.exception(f"Exception while processing Target.getTargets response: {e}")
                return None

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
                elif "error" in data and data.get("id") == attach_cmd_id:
                    logger.error(f"Error in Target.attachToTarget response: {data['error']}")
                    return None
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for Target.attachToTarget response for target {target_id}.")
                return None
            except Exception as e:
                logger.exception(f"Exception while processing Target.attachToTarget response: {e}")
                return None

    async def connect(self):
        if self.websocket_connection and self.session_id and self.is_page_initialized:
            logger.debug("connect() called, but already connected and initialized.")
            return True
        logger.info("Attempting to connect and initialize Copilot client...")
        if platform.system() != "Windows":
            logger.error("Platform is not Windows. This script is designed for Windows only.")
            raise RuntimeError("This script is designed for Windows only.")
        if self.debug_profile_dir and not os.path.exists(self.debug_profile_dir):
            try:
                os.makedirs(self.debug_profile_dir)
                logger.info(f"Created debug profile directory: {self.debug_profile_dir}")
            except OSError as e:
                logger.warning(f"Could not create profile directory {self.debug_profile_dir}: {e}")
        else:
            logger.debug(f"Debug profile directory already exists or not specified: {self.debug_profile_dir}")

        try:
            logger.info(f"Starting Edge: {self.edge_path} with port {self.debugging_port}")
            edge_args = [self.edge_path, f"--remote-debugging-port={self.debugging_port}", "--no-first-run",
                         "--no-default-browser-check", "--no-restore-session-state", "--restore-last-session=false",
                         "--disable-session-crashed-bubble"]
            if self.debug_profile_dir: edge_args.append(f"--user-data-dir={self.debug_profile_dir}")
            self.edge_process = subprocess.Popen(edge_args)
            await asyncio.sleep(5) # Give Edge time to start
        except FileNotFoundError:
            logger.error(f"Edge executable not found at {self.edge_path}")
            raise RuntimeError(f"Edge executable not found at {self.edge_path}")
        except Exception as e:
            logger.exception(f"Error starting Edge: {str(e)}")
            raise RuntimeError(f"Error starting Edge: {str(e)}")

        version_url = f"http://127.0.0.1:{self.debugging_port}/json/version"
        self.browser_ws_url = None # Initialize before loop
        for attempt in range(10):
            try:
                with urllib.request.urlopen(version_url, timeout=5) as response:
                    if response.status == 200:
                        self.browser_ws_url = json.loads(response.read().decode()).get("webSocketDebuggerUrl")
                        if self.browser_ws_url:
                            logger.info(f"Fetched browser WebSocket URL: {self.browser_ws_url}")
                            break # Exit loop if successful
            except Exception as e_fetch:
                logger.debug(f"Attempt {attempt + 1} to fetch debugger URL failed: {e_fetch}")
            
            if self.browser_ws_url: # Check again in case break was hit inside try
                break

            if attempt < 9: # If not the last attempt and not successful
                logger.debug(f"Waiting 2s before retry for debugger URL (attempt {attempt+2}/10)") # attempt+2 for 1-based display
                await asyncio.sleep(2) # Wait before retrying
        
        if not self.browser_ws_url: # After all attempts
            logger.error("Could not fetch debugger URL for Edge after multiple attempts.")
            raise RuntimeError("Could not fetch debugger URL for Edge after multiple attempts.")

        try:
            if not self.browser_ws_url: # Should be caught by the check above, but as a safeguard
                logger.error("Browser WebSocket URL is None before attempting to connect (safeguard).")
                raise RuntimeError("Browser WebSocket URL is None before attempting to connect.")
            
            logger.info(f"Connecting to browser WebSocket: {self.browser_ws_url}")
            self.websocket_connection = await websockets.connect(self.browser_ws_url, ping_interval=None, ping_timeout=None)
            logger.info("Browser WebSocket connected. Finding page target...")
            self.target_id = await self._find_page_target(self.websocket_connection)
            if not self.target_id:
                logger.error("Could not find a suitable page target.")
                raise RuntimeError("Could not find a suitable page target.")
            logger.info(f"Page target found: {self.target_id}. Attaching to target...")
            self.session_id = await self._attach_to_target(self.websocket_connection, self.target_id)
            if not self.session_id:
                logger.error("Failed to attach to the page target.")
                raise RuntimeError("Failed to attach to the page target.")
            logger.info(f"Attached to target with session ID: {self.session_id}")

            if not self.is_page_initialized:
                logger.info(f"Navigating to Copilot URL: {self.copilot_url}")
                await self._send_cdp_command(self.websocket_connection, "Page.navigate", {"url": self.copilot_url}, session_id=self.session_id)
                page_load_timeout = 3.0 # Reduced timeout
                load_event_fired = False; start_load_wait = time.time()
                logger.debug(f"Waiting for Page.loadEventFired (timeout: {page_load_timeout}s)...")
                while time.time() - start_load_wait < page_load_timeout:
                    try:
                        message_str = await asyncio.wait_for(self.websocket_connection.recv(), timeout=1.0)
                        event_data = json.loads(message_str)
                        if event_data.get("method") == "Page.loadEventFired" and event_data.get("sessionId") == self.session_id:
                            logger.info("Page.loadEventFired received.")
                            load_event_fired = True; break
                    except asyncio.TimeoutError: pass # Expected during wait
                    except json.JSONDecodeError as e_json: logger.warning(f"Non-JSON message received while waiting for page load: {message_str[:100]}... Error: {e_json}")
                    except Exception as e_recv: logger.warning(f"Exception while waiting for page load event: {e_recv}") ; break # Break on other errors
                if not load_event_fired: logger.debug(f"Page.loadEventFired not received within {page_load_timeout}s.") # Changed to DEBUG
                
                logger.info("Enabling CDP domains: Page, Network, Runtime, DOM.")
                await asyncio.sleep(2) # Give page a moment to settle after load event (or timeout)
                for domain in ["Page", "Network", "Runtime", "DOM"]:
                    await self._send_cdp_command(self.websocket_connection, f"{domain}.enable", {}, session_id=self.session_id)
                self.is_page_initialized = True; self.is_first_message_sent = False
                logger.info("Page initialized and CDP domains enabled.")
            logger.info("Copilot client connection successful.")
            return True
        except Exception as e:
            logger.exception(f"Error in connect setup's CDP/WebSocket phase: {str(e)}")
            await self.close() # Ensure cleanup on error
            raise RuntimeError(f"Error in connect setup's CDP/WebSocket phase: {str(e)}")

    async def _capture_chat_websocket_id(self, ws, session_id):
        logger.info("Starting WebSocket ID capture task...")
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
                            logger.info(f"Captured chat WebSocket ID (in task): {self.chat_websocket_request_id}")
                            return
                except asyncio.TimeoutError: pass # Expected during wait
                except json.JSONDecodeError: logger.warning("Warning (capture task): Non-JSON message received.")
                except Exception as inner_e: logger.warning(f"Inner exception in capture task: {inner_e}"); await asyncio.sleep(0.1)
            logger.warning(f"Capture task: WebSocket ID not found in {timeout}s.")
        except asyncio.CancelledError: logger.info("WebSocket ID capture task cancelled."); raise
        except Exception as e: logger.exception(f"Exception in _capture_chat_websocket_id: {e}")

    def _format_prompt_for_log(self, prompt: str, max_len: int = 100) -> str:
        """Formats the prompt string for logging, showing total length and truncating if not in debug mode."""
        total_len = len(prompt)
        if self.is_debug_logging or total_len <= max_len:
            return f"(len:{total_len}) '{prompt}'"
        
        truncated_prompt = prompt[:max_len].replace('\n', ' ')
        return f"(len:{total_len}) '{truncated_prompt}...'"

    async def send_message_and_get_response(self, user_message: str):
        if not self.is_page_initialized or not self.websocket_connection or not self.session_id:
            logger.error("Copilot client not connected or page not initialized.")
            raise RuntimeError("Copilot client not connected or page not initialized.")

        ws = self.websocket_connection; session_id = self.session_id
        
        logger.info(f"Simulating typing: {self._format_prompt_for_log(user_message)}") # Removed extra single quotes around the formatted string
        
        doc_root_id_cmd = await self._send_cdp_command(ws, "DOM.getDocument", {"depth": -1}, session_id=session_id)
        root_node_id = None
        try:
            while True:
                message_str = await asyncio.wait_for(ws.recv(), timeout=10.0)
                data = json.loads(message_str)
                if data.get("id") == doc_root_id_cmd:
                    if "result" in data and data["result"].get("root"): root_node_id = data["result"]["root"]["nodeId"]
                    elif "error" in data:
                        logger.error(f"CDP Error (DOM.getDocument): {data['error'].get('message')}")
                        raise RuntimeError(f"CDP Error (DOM.getDocument): {data['error'].get('message')}")
                    break
        except Exception as e:
            logger.exception(f"Error getting document root: {str(e)}")
            raise RuntimeError(f"Error getting document root: {str(e)}")
        if not root_node_id:
            logger.error("Failed to obtain document root node ID.")
            raise RuntimeError("Failed to obtain document root node ID.")

        query_selector_id_cmd = await self._send_cdp_command(ws, "DOM.querySelector", {"nodeId": root_node_id, "selector": self.user_input_selector}, session_id=session_id)
        textarea_node_id = None
        try:
            while True:
                message_str = await asyncio.wait_for(ws.recv(), timeout=10.0)
                data = json.loads(message_str)
                if data.get("id") == query_selector_id_cmd:
                    if "result" in data and data["result"].get("nodeId") is not None:
                        textarea_node_id = data["result"]["nodeId"]
                        if textarea_node_id == 0:
                            logger.error(f"Textarea '{self.user_input_selector}' not found (nodeId is 0).")
                            raise RuntimeError(f"Textarea '{self.user_input_selector}' not found.")
                    elif "error" in data:
                        logger.error(f"CDP Error (DOM.querySelector for '{self.user_input_selector}'): {data['error'].get('message')}")
                        raise RuntimeError(f"CDP Error (DOM.querySelector): {data['error'].get('message')}")
                    break
        except Exception as e:
            logger.exception(f"Error querying selector '{self.user_input_selector}': {str(e)}")
            raise RuntimeError(f"Error querying selector '{self.user_input_selector}': {str(e)}")
        if not textarea_node_id: # Should be caught by nodeId == 0 check if selector is bad, but as safeguard
            logger.error(f"Failed to obtain textarea node ID for '{self.user_input_selector}'.")
            raise RuntimeError(f"Failed to obtain textarea node ID for '{self.user_input_selector}'.")
        
        await self._send_cdp_command(ws, "DOM.focus", {"nodeId": textarea_node_id}, session_id=session_id)
        await asyncio.sleep(0.5) # Allow time for focus
        await self._send_cdp_command(ws, "Input.insertText", {"text": user_message}, session_id=session_id)
        logger.debug("Text insertion command sent.")
        await asyncio.sleep(0.5) # Allow time for text to be processed by JS if any

        capture_task = None
        if self.chat_websocket_request_id is None:
            logger.info("chat_websocket_request_id is None. Starting WebSocket ID capture task...")
            capture_task = asyncio.create_task(self._capture_chat_websocket_id(ws, session_id))
        else:
            logger.info(f"Reusing existing chat_websocket_request_id: {self.chat_websocket_request_id}")

        logger.info(f"Clicking submit button: '{self.submit_button_selector}'")
        js_click = f"document.querySelector('{self.submit_button_selector}')?.click();"
        await self._send_cdp_command(ws, "Runtime.evaluate", {"expression": js_click, "awaitPromise": False}, session_id=session_id)
        await asyncio.sleep(0.2) 

        if capture_task:
            logger.info("Waiting for WebSocket ID capture task to complete...")
            try:
                await asyncio.wait_for(capture_task, timeout=20.0) 
                if self.chat_websocket_request_id is None and not capture_task.cancelled():
                    logger.error("WebSocket ID capture task completed but ID is still None.")
                    raise RuntimeError("WebSocket ID capture task completed but ID is still None.")
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for WebSocket ID capture task to complete.")
                if not capture_task.done(): capture_task.cancel()
                if self.chat_websocket_request_id is None: 
                    logger.error("Timeout occurred and chat WebSocket Request ID is still None.")
                    raise RuntimeError("Timeout waiting for chat WebSocket Request ID capture task.")
            except asyncio.CancelledError:
                logger.warning("WebSocket ID capture task was cancelled.")
                if self.chat_websocket_request_id is None: 
                    logger.error("Capture task cancelled and chat WebSocket Request ID is still None.")
                    raise RuntimeError("Chat WebSocket Request ID capture was cancelled before completion.")
            except Exception as e_capture_wait:
                logger.exception(f"Error waiting for WebSocket ID capture task: {e_capture_wait}")
                if not capture_task.done(): capture_task.cancel()
                if self.chat_websocket_request_id is None:
                    raise RuntimeError(f"Error during WebSocket ID capture: {str(e_capture_wait)}")
        
        if not self.chat_websocket_request_id:
            logger.error("Failed to obtain chat_websocket_request_id for monitoring.")
            raise RuntimeError("Failed to obtain chat_websocket_request_id for monitoring.")

        target_websocket_request_id = self.chat_websocket_request_id
        logger.info(f"Monitoring WebSocket frames for Request ID: {target_websocket_request_id}...")
        try:
            while True:
                message_str = await ws.recv()
                data = json.loads(message_str)
                if data.get("sessionId") == session_id and "method" in data and \
                   data.get("params", {}).get("requestId") == target_websocket_request_id:
                    if data["method"] == "Network.webSocketFrameReceived":
                        try:
                            payload_data_str = data["params"]['response']['payloadData']
                            if self.is_debug_logging:
                                logger.debug(f"Raw WS payload for {target_websocket_request_id}: {payload_data_str[:200]}...")
                            payload_json = json.loads(payload_data_str)
                            event_type = payload_json.get("event")
                            if event_type == "appendText":
                                text_chunk = payload_json.get("text", "")
                                yield text_chunk
                            elif event_type == "done":
                                logger.info("Response complete ('done' event).")
                                return
                            elif event_type: 
                                logger.debug(f"[Received Event: {event_type}] Data: {payload_json}")
                        except json.JSONDecodeError as e_json_payload:
                            logger.warning(f"[Error decoding JSON payload: {e_json_payload}] Payload: {payload_data_str[:200]}...")
                        except Exception as e_recv:
                            logger.exception(f"[Error processing Received Frame: {e_recv}]")
        except websockets.exceptions.ConnectionClosed as e_conn_closed:
            logger.info(f"Browser WebSocket closed (monitoring): {e_conn_closed}") 
        except Exception as e_mon:
            logger.exception(f"Error during WebSocket monitoring: {str(e_mon)}")
            raise RuntimeError(f"Error during WebSocket monitoring: {str(e_mon)}")
        finally:
            if not self.is_first_message_sent:
                self.is_first_message_sent = True
                logger.debug("Marked first message as sent.")

    async def close(self):
        logger.info("Closing Copilot client...")
        if self.websocket_connection:
            try:
                logger.debug("Closing browser WebSocket connection...")
                await self.websocket_connection.close()
                logger.info("Browser WebSocket connection closed.")
            except Exception as e_ws_close:
                logger.warning(f"Exception during WebSocket close: {e_ws_close}")
            self.websocket_connection = None
        if self.edge_process:
            try:
                logger.debug(f"Terminating Edge process (PID: {self.edge_process.pid if self.edge_process else 'N/A'})...")
                if self.edge_process: # Ensure it's not None
                    self.edge_process.terminate()
                    self.edge_process.wait(timeout=2) # Wait for graceful termination
                logger.info("Edge process terminated.")
            except subprocess.TimeoutExpired:
                logger.warning("Timeout waiting for Edge process to terminate. Killing process.")
                if self.edge_process: self.edge_process.kill()
                logger.info("Edge process killed due to timeout.")
            except Exception as e_term:
                logger.warning(f"Exception during Edge process terminate/wait: {e_term}. Attempting to kill.")
                if self.edge_process:
                    try:
                        self.edge_process.kill()
                        logger.info("Edge process killed after failed termination.")
                    except Exception as e_kill:
                        logger.error(f"Exception during Edge process kill: {e_kill}")
            self.edge_process = None
        self.session_id = self.browser_ws_url = self.target_id = self.chat_websocket_request_id = None
        self.is_page_initialized = self.is_first_message_sent = False
        logger.info("Copilot client session attributes reset and Edge process handled.")