import asyncio
import json
import subprocess
import websockets # type: ignore
# Removed explicit import of WebSocketClientProtocol, will use string literal for type hint
import time
import platform
import os
import urllib.request
import urllib.error
import typing
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger("CopilotClient.Base")

class BaseCopilotClient(ABC):
    def __init__(self,
                 edge_path: str,
                 debug_profile_dir: typing.Optional[str],
                 debugging_port: int,
                 is_debug_logging: bool = False):
        self.edge_path = edge_path
        self.debug_profile_dir = debug_profile_dir
        self.debugging_port = debugging_port
        self.is_debug_logging = is_debug_logging

        self._cdp_message_id_counter = 0
        self.edge_process: typing.Optional[subprocess.Popen] = None
        self.browser_cdp_ws: typing.Optional[typing.Any] = None # Using typing.Any to bypass Pylance issue
        self.browser_cdp_url: typing.Optional[str] = None
        self.page_target_id: typing.Optional[str] = None
        self.page_cdp_session_id: typing.Optional[str] = None
        
        self.is_browser_cdp_connected: bool = False
        self.is_page_initialized: bool = False # Tracks if Page.navigate and CDP domains are enabled

    async def _send_cdp_command(self, ws: typing.Any, method: str, params: typing.Optional[dict] = None, session_id: typing.Optional[str] = None) -> int: # Using typing.Any for ws parameter
        if params is None:
            params = {}
        self._cdp_message_id_counter += 1
        current_id = self._cdp_message_id_counter
        msg = {"id": current_id, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        
        if self.is_debug_logging:
            log_params = json.dumps(params) if params else "{}"
            logger.debug(f"Sending CDP: ID={current_id}, Method={method}, Params={log_params}, SessionID={session_id}")
        
        await ws.send(json.dumps(msg))
        return current_id

    async def _launch_edge_if_needed(self) -> bool:
        if self.edge_process and self.edge_process.poll() is None:
            logger.debug("Edge process already running.")
            return True

        logger.info(f"Starting Edge: {self.edge_path} with remote debugging port {self.debugging_port}")
        if platform.system() != "Windows":
            logger.error("Platform is not Windows. This script is designed for Windows only.")
            raise RuntimeError("This script is designed for Windows only.")

        if self.debug_profile_dir:
            if not os.path.exists(self.debug_profile_dir):
                try:
                    os.makedirs(self.debug_profile_dir)
                    logger.info(f"Created debug profile directory: {self.debug_profile_dir}")
                except OSError as e:
                    logger.error(f"Could not create profile directory {self.debug_profile_dir}: {e}")
                    return False
            else:
                logger.debug(f"Debug profile directory already exists: {self.debug_profile_dir}")
        
        edge_args = [
            self.edge_path,
            f"--remote-debugging-port={self.debugging_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-restore-session-state", # Prevents restoring previous session
            "--restore-last-session=false", # Double ensure no session restore
            "--disable-session-crashed-bubble"
        ]
        if self.debug_profile_dir:
            edge_args.append(f"--user-data-dir={self.debug_profile_dir}")

        try:
            self.edge_process = subprocess.Popen(edge_args)
            await asyncio.sleep(5)  # Give Edge some time to start up
            if self.edge_process.poll() is not None:
                logger.error(f"Edge process terminated unexpectedly after launch. Exit code: {self.edge_process.returncode}")
                self.edge_process = None
                return False
            logger.info(f"Edge process started (PID: {self.edge_process.pid}).")
            return True
        except FileNotFoundError:
            logger.error(f"Edge executable not found at {self.edge_path}")
            self.edge_process = None
            return False
        except Exception as e:
            logger.exception(f"Error starting Edge: {e}")
            self.edge_process = None
            return False

    async def _get_browser_cdp_url(self) -> typing.Optional[str]:
        version_url = f"http://127.0.0.1:{self.debugging_port}/json/version"
        for attempt in range(10): # Retry up to 10 times
            try:
                with urllib.request.urlopen(version_url, timeout=3) as response:
                    if response.status == 200:
                        data = json.loads(response.read().decode())
                        self.browser_cdp_url = data.get("webSocketDebuggerUrl")
                        if self.browser_cdp_url:
                            logger.info(f"Fetched browser CDP WebSocket URL: {self.browser_cdp_url}")
                            return self.browser_cdp_url
            except urllib.error.URLError as e:
                logger.debug(f"Attempt {attempt + 1}/10 to fetch debugger URL failed (URLError): {e}")
            except json.JSONDecodeError as e:
                logger.warning(f"Attempt {attempt + 1}/10: Error decoding JSON from version endpoint: {e}")
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1}/10: Unexpected error fetching debugger URL: {e}")
            
            if attempt < 9:
                await asyncio.sleep(2) # Wait before retrying
        
        logger.error("Could not fetch browser CDP URL after multiple attempts.")
        return None

    async def _connect_to_browser_cdp_ws(self) -> bool:
        if not self.browser_cdp_url and not await self._get_browser_cdp_url():
            return False
        
        if not self.browser_cdp_url: # Should be caught above, but defensive
             logger.error("Browser CDP URL is still None before attempting to connect.")
             return False

        try:
            logger.info(f"Connecting to browser CDP WebSocket: {self.browser_cdp_url}")
            # Set a timeout for the connection attempt itself
            self.browser_cdp_ws = await asyncio.wait_for(
                websockets.connect(self.browser_cdp_url, ping_interval=None, ping_timeout=None, max_size=10 * 1024 * 1024), # Increased max_size to 10MB
                timeout=10.0
            )
            self.is_browser_cdp_connected = True
            logger.info("Successfully connected to browser CDP WebSocket.")
            return True
        except asyncio.TimeoutError:
            logger.error(f"Timeout connecting to browser CDP WebSocket: {self.browser_cdp_url}")
        except websockets.exceptions.InvalidURI:
            logger.error(f"Invalid URI for browser CDP WebSocket: {self.browser_cdp_url}")
        except websockets.exceptions.ConnectionClosedError as e:
            logger.error(f"Browser CDP WebSocket connection closed unexpectedly during connect: {e}")
        except Exception as e:
            logger.exception(f"Error connecting to browser CDP WebSocket: {e}")
        
        self.browser_cdp_ws = None
        self.is_browser_cdp_connected = False
        return False

    async def _find_page_target_and_attach(self) -> bool:
        if not self.is_browser_cdp_connected or not self.browser_cdp_ws:
            logger.error("Cannot find page target: Browser CDP WebSocket not connected.")
            return False

        list_targets_cmd_id = await self._send_cdp_command(self.browser_cdp_ws, "Target.getTargets")
        try:
            while True:
                message_str = await asyncio.wait_for(self.browser_cdp_ws.recv(), timeout=10.0)
                data = json.loads(message_str)
                if data.get("id") == list_targets_cmd_id:
                    if "result" in data and "targetInfos" in data["result"]:
                        for target_info in data["result"]["targetInfos"]:
                            if target_info.get("type") == "page" and not target_info.get("url", "").startswith("devtools://"):
                                self.page_target_id = target_info["targetId"]
                                logger.info(f"Found page target: ID={self.page_target_id}, URL={target_info.get('url')}")
                                break
                        if not self.page_target_id:
                            logger.warning("No suitable page target found in Target.getTargets response.")
                            return False
                        break # Found target or no suitable target
                    elif "error" in data:
                        logger.error(f"Error in Target.getTargets response: {data['error']}")
                        return False
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for Target.getTargets response.")
            return False
        except Exception as e:
            logger.exception(f"Exception processing Target.getTargets response: {e}")
            return False
        
        if not self.page_target_id: return False # Should be caught above

        attach_cmd_id = await self._send_cdp_command(self.browser_cdp_ws, "Target.attachToTarget", {"targetId": self.page_target_id, "flatten": True})
        try:
            while True:
                message_str = await asyncio.wait_for(self.browser_cdp_ws.recv(), timeout=10.0)
                data = json.loads(message_str)
                if data.get("id") == attach_cmd_id and "result" in data and "sessionId" in data["result"]:
                    self.page_cdp_session_id = data["result"]["sessionId"]
                    logger.info(f"Attached to page target {self.page_target_id} with session ID: {self.page_cdp_session_id}")
                    return True
                # Sometimes, the session ID comes in an event before the command result
                elif data.get("method") == "Target.attachedToTarget" and \
                     data.get("params", {}).get("targetInfo", {}).get("targetId") == self.page_target_id and \
                     "sessionId" in data.get("params", {}):
                    self.page_cdp_session_id = data["params"]["sessionId"]
                    logger.info(f"Attached to page target {self.page_target_id} via event with session ID: {self.page_cdp_session_id}")
                    return True
                elif "error" in data and data.get("id") == attach_cmd_id:
                    logger.error(f"Error in Target.attachToTarget response: {data['error']}")
                    return False
        except asyncio.TimeoutError:
            logger.warning(f"Timeout waiting for Target.attachToTarget response for target {self.page_target_id}.")
            return False
        except Exception as e:
            logger.exception(f"Exception processing Target.attachToTarget response: {e}")
            return False
        return False # Should not be reached if logic is correct

    async def connect_to_browser_and_page(self) -> bool:
        """Launches Edge, connects to browser CDP, finds a page target, and attaches to it."""
        if self.is_browser_cdp_connected and self.page_cdp_session_id:
            logger.debug("Already connected to browser and attached to page target.")
            return True

        if not await self._launch_edge_if_needed():
            return False
        if not await self._connect_to_browser_cdp_ws():
            await self.close(error_context="Failed to connect to browser CDP WebSocket") # Cleanup Edge if CDP connection failed
            return False
        if not await self._find_page_target_and_attach():
            await self.close(error_context="Failed to find page target and attach") # Cleanup Edge and CDP if page attach failed
            return False
        
        logger.info("Successfully connected to browser and attached to page target.")
        return True

    async def _navigate_and_initialize_cdp_domains(self, page_url: str, critical_element_selector: str) -> bool:
        """
        Navigates to the specified URL, waits for a critical element to be present,
        and then enables common CDP domains.
        """
        if not self.is_browser_cdp_connected or not self.browser_cdp_ws or not self.page_cdp_session_id:
            logger.error("Cannot initialize page: Not connected to browser CDP or not attached to page.")
            return False

        logger.info(f"Navigating to page URL: {page_url}")
        nav_cmd_id = await self._send_cdp_command(self.browser_cdp_ws, "Page.navigate", {"url": page_url}, session_id=self.page_cdp_session_id)
        
        navigation_timeout = 15.0  # Timeout for Page.navigate command itself to succeed/fail
        navigate_success = False
        start_nav_wait = time.monotonic()

        # First, wait for the navigation command to complete (either success or error)
        logger.debug(f"Waiting for Page.navigate command result (timeout: {navigation_timeout}s)...")
        try:
            while time.monotonic() - start_nav_wait < navigation_timeout:
                message_str = await asyncio.wait_for(self.browser_cdp_ws.recv(), timeout=5.0) # Keep this timeout at 5.0
                event_data = json.loads(message_str)
                if event_data.get("id") == nav_cmd_id:
                    if "result" in event_data:
                        logger.info(f"Page.navigate command successful: {event_data['result']}")
                        navigate_success = True
                        break
                    elif "error" in event_data:
                        logger.error(f"Page.navigate command failed: {event_data['error']}")
                        return False # Navigation itself failed, abort
                # Optionally, listen for Page.loadEventFired here too for logging, but don't block on it.
                if event_data.get("method") == "Page.loadEventFired" and event_data.get("sessionId") == self.page_cdp_session_id:
                    logger.info("Page.loadEventFired received during navigation wait.") # For info only
            if not navigate_success: # If loop finished due to timeout without success
                logger.error(f"Page.navigate command for {page_url} did not return success within {navigation_timeout}s.")
                return False
        except asyncio.TimeoutError: # Timeout for the ws.recv() within the loop
            logger.error(f"Timeout receiving response for Page.navigate command for {page_url}.")
            return False
        except json.JSONDecodeError as e_json:
            logger.error(f"Failed to decode JSON response for Page.navigate: {e_json}")
            return False
        except Exception as e_nav:
            logger.exception(f"Unexpected error waiting for Page.navigate result: {e_nav}")
            return False

        # If navigation was successful, now poll for the critical element
        logger.info(f"Navigation to {page_url} successful. Waiting for critical element '{critical_element_selector}'...")
        element_wait_timeout = 25.0  # Total time to wait for the element after successful navigation
        check_interval = 0.5       # How often to check for the element
        start_element_wait = time.monotonic()
        critical_element_found = False
        
        # Also, listen for Page.loadEventFired in a non-blocking way during element check for logging
        load_event_fired_logged = False

        while time.monotonic() - start_element_wait < element_wait_timeout:
            # Check for critical element
            root_node_id = await self._get_document_root_node_id() # This has its own internal timeout
            if root_node_id:
                element_node_id = await self._query_selector_node_id(root_node_id, critical_element_selector) # Also has internal timeout
                if element_node_id:
                    logger.info(f"Critical element '{critical_element_selector}' found (nodeId: {element_node_id}).")
                    critical_element_found = True
                    break
            
            # Non-blocking check for Page.loadEventFired for logging
            if not load_event_fired_logged:
                try:
                    # Try to receive a message with a very short timeout, so it doesn't block element checking much
                    message_str = await asyncio.wait_for(self.browser_cdp_ws.recv(), timeout=0.01)
                    event_data = json.loads(message_str)
                    if event_data.get("method") == "Page.loadEventFired" and event_data.get("sessionId") == self.page_cdp_session_id:
                        logger.info("Page.loadEventFired received while polling for critical element.")
                        load_event_fired_logged = True
                except (asyncio.TimeoutError, json.JSONDecodeError):
                    pass # Expected if no message or non-JSON message

            if not critical_element_found: # Only sleep if element not yet found
                logger.debug(f"Critical element '{critical_element_selector}' not yet found. Retrying in {check_interval}s...")
                await asyncio.sleep(check_interval)

        if not critical_element_found:
            logger.error(f"Critical element '{critical_element_selector}' not found after {element_wait_timeout}s. Page may not be fully interactive.")
            return False

        # Critical element found, now enable domains
        logger.info("Enabling CDP domains: Page, Network, Runtime, DOM.")
        await asyncio.sleep(0.1) # Brief pause before enabling domains
        for domain in ["Page", "Network", "Runtime", "DOM"]:
            await self._send_cdp_command(self.browser_cdp_ws, f"{domain}.enable", {}, session_id=self.page_cdp_session_id)
        
        self.is_page_initialized = True
        logger.info(f"Page initialized at {page_url}, critical element '{critical_element_selector}' found, and CDP domains enabled.")
        return True

    async def _get_document_root_node_id(self) -> typing.Optional[int]:
        if not self.browser_cdp_ws or not self.page_cdp_session_id: return None
        cmd_id = await self._send_cdp_command(self.browser_cdp_ws, "DOM.getDocument", {"depth": 1}, session_id=self.page_cdp_session_id) # Changed depth from -1 to 1
        try:
            while True:
                msg_str = await asyncio.wait_for(self.browser_cdp_ws.recv(), timeout=5.0)
                data = json.loads(msg_str)
                if data.get("id") == cmd_id:
                    if "result" in data and data["result"].get("root"):
                        return data["result"]["root"]["nodeId"]
                    logger.error(f"Error in DOM.getDocument response: {data.get('error')}")
                    return None
        except Exception as e:
            logger.exception(f"Exception getting document root: {e}")
            return None
        return None # Should be unreachable

    async def _query_selector_node_id(self, parent_node_id: int, selector: str) -> typing.Optional[int]:
        if not self.browser_cdp_ws or not self.page_cdp_session_id: return None
        cmd_id = await self._send_cdp_command(self.browser_cdp_ws, "DOM.querySelector", {"nodeId": parent_node_id, "selector": selector}, session_id=self.page_cdp_session_id)
        try:
            while True:
                msg_str = await asyncio.wait_for(self.browser_cdp_ws.recv(), timeout=5.0)
                data = json.loads(msg_str)
                if data.get("id") == cmd_id:
                    if "result" in data and data["result"].get("nodeId") != 0: # 0 means not found
                        return data["result"]["nodeId"]
                    elif "result" in data and data["result"].get("nodeId") == 0:
                        logger.warning(f"Selector '{selector}' not found (nodeId 0).")
                        return None
                    logger.error(f"Error in DOM.querySelector for '{selector}': {data.get('error')}")
                    return None
        except Exception as e:
            logger.exception(f"Exception querying selector '{selector}': {e}")
            return None
        return None # Should be unreachable
        
    async def _focus_node_via_cdp(self, node_id: int) -> bool:
        if not self.browser_cdp_ws or not self.page_cdp_session_id: return False
        await self._send_cdp_command(self.browser_cdp_ws, "DOM.focus", {"nodeId": node_id}, session_id=self.page_cdp_session_id)
        # DOM.focus doesn't have a direct success/failure response in the command result itself.
        # We assume it works if the command is sent. For more robust check, one might query focused element.
        logger.debug(f"Sent DOM.focus command for nodeId {node_id}")
        await asyncio.sleep(0.1) # Small delay for focus to take effect
        return True

    async def _insert_text_via_cdp(self, text_to_insert: str) -> bool:
        if not self.browser_cdp_ws or not self.page_cdp_session_id: return False
        # Assumes the correct element is already focused.
        await self._send_cdp_command(self.browser_cdp_ws, "Input.insertText", {"text": text_to_insert}, session_id=self.page_cdp_session_id)
        logger.debug(f"Sent Input.insertText command for text: '{text_to_insert[:50]}...'")
        await asyncio.sleep(0.1) # Small delay for text insertion
        return True

    async def _click_element_via_js(self, selector: str) -> bool:
        if not self.browser_cdp_ws or not self.page_cdp_session_id:
            logger.error(f"Cannot click element '{selector}': Browser CDP WebSocket not connected or page session ID missing.")
            return False
        
        js_click_expression = f"document.querySelector('{selector}')?.click();"
        logger.debug(f"Sending JS click command for selector: {selector}")
        
        # Send the command but do not wait for its specific result here to avoid ConcurrencyError
        # if another task (like _capture_chat_websocket_id) is also calling recv().
        # The success of the click will be implicitly determined by subsequent events (e.g., WebSocket creation).
        await self._send_cdp_command(
            self.browser_cdp_ws,
            "Runtime.evaluate",
            {"expression": js_click_expression, "awaitPromise": False, "userGesture": True, "returnByValue": False}, # Added userGesture
            session_id=self.page_cdp_session_id
        )
        # We assume the click command was sent successfully.
        # If Runtime.evaluate itself had an issue that _send_cdp_command doesn't catch (unlikely for fire-and-forget),
        # it would be an unhandled exception from ws.send().
        # The actual effect of the click (e.g., a new WebSocket being created) will be observed by other parts of the code.
        logger.info(f"JS click command for '{selector}' sent.")
        return True # Optimistically return True, actual success is determined by subsequent behavior.


    def _format_prompt_for_log(self, prompt: str, max_len: int = 100) -> str:
        """Formats the prompt string for logging, showing total length and truncating if not in debug mode."""
        total_len = len(prompt)
        if self.is_debug_logging or total_len <= max_len:
            # For full prompt or short prompts, show it as is.
            # Replacing newlines might make it less readable if it's a multi-line prompt being shown fully.
            if self.is_debug_logging:
                return f"(len:{total_len}) '{prompt}'" # Show full prompt as is in debug
            else: # Short prompt, not in debug
                 prompt_oneline = prompt.replace('\n', ' ')
                 return f"(len:{total_len}) '{prompt_oneline}'"
        
        truncated_prompt = prompt[:max_len].replace('\n', ' ')
        return f"(len:{total_len}) '{truncated_prompt}...'"

    @abstractmethod
    async def connect(self) -> bool:
        """
        Connects to the specific Copilot service.
        This should typically call self.connect_to_browser_and_page()
        and then self._navigate_and_initialize_cdp_domains(self.copilot_url, self.user_input_selector).
        Subclasses will define self.copilot_url and self.user_input_selector.
        """
        pass

    @abstractmethod
    async def send_message_and_get_response(self, user_message: str) -> typing.AsyncGenerator[str, None]:
        """
        Sends a message to the Copilot service and yields response chunks.
        This is the primary method to be implemented by subclasses for chat interaction.
        It must be an async generator.
        """
        # Required to make it an async generator, subclasses will `yield actual_data`
        if False: # pragma: no cover
            yield ""

    @abstractmethod
    async def reinitialize_page_session(self) -> bool:
        """
        Reinitializes the current page session.
        This typically involves resetting client-specific state related to the page
        and then calling self._navigate_and_initialize_cdp_domains() to reload the page.
        Returns True if successful, False otherwise.
        """
        pass

    async def close(self, error_context: typing.Optional[str] = None):
        if error_context:
            print(f"\nAn error occurred: {error_context}")
            print("The browser is about to close.")
            try:
                input("Press Enter to continue and close the browser...")
            except KeyboardInterrupt:
                print("\nKeyboard interrupt received. Closing browser immediately.")
            except EOFError: # Happens if stdin is not available (e.g. running as a service)
                logger.warning("EOFError encountered waiting for user input. Proceeding with browser close.")

        logger.info("Closing BaseCopilotClient...")
        if self.browser_cdp_ws:
            try:
                logger.debug("Closing browser CDP WebSocket connection...")
                await self.browser_cdp_ws.close()
                logger.info("Browser CDP WebSocket connection closed.")
            except Exception as e_ws_close:
                logger.warning(f"Exception during browser CDP WebSocket close: {e_ws_close}")
            finally:
                self.browser_cdp_ws = None
                self.is_browser_cdp_connected = False
                self.page_cdp_session_id = None
                self.page_target_id = None
                self.is_page_initialized = False

        if self.edge_process:
            pid = self.edge_process.pid
            logger.debug(f"Terminating Edge process (PID: {pid})...")
            try:
                if self.edge_process.poll() is None: # Check if process is still running
                    self.edge_process.terminate()
                    try:
                        await asyncio.wait_for(asyncio.to_thread(self.edge_process.wait), timeout=5.0)
                        logger.info(f"Edge process (PID: {pid}) terminated gracefully.")
                    except asyncio.TimeoutError:
                        logger.warning(f"Timeout waiting for Edge process (PID: {pid}) to terminate. Killing.")
                        self.edge_process.kill()
                        await asyncio.to_thread(self.edge_process.wait) # Ensure kill completes
                        logger.info(f"Edge process (PID: {pid}) killed.")
                else:
                    logger.info(f"Edge process (PID: {pid}) already terminated (exit code: {self.edge_process.returncode}).")
            except Exception as e_term:
                logger.warning(f"Exception during Edge process (PID: {pid}) termination: {e_term}. Attempting kill.")
                if self.edge_process.poll() is None:
                    try:
                        self.edge_process.kill()
                        await asyncio.to_thread(self.edge_process.wait)
                        logger.info(f"Edge process (PID: {pid}) killed after failed termination attempt.")
                    except Exception as e_kill:
                        logger.error(f"Exception during Edge process (PID: {pid}) kill: {e_kill}")
            finally:
                self.edge_process = None
        
        logger.info("BaseCopilotClient closed and resources reset.")