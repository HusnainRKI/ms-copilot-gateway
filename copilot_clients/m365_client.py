import asyncio
import json
import websockets # type: ignore
import time
import typing
import logging

from .base_client import BaseCopilotClient

logger = logging.getLogger("CopilotClient.M365")

# Record Separator character
RS = "\x1e"

class M365CopilotClient(BaseCopilotClient):
    def __init__(self,
                 edge_path: str,
                 debug_profile_dir: typing.Optional[str],
                 debugging_port: int,
                 copilot_url: str, # MS365 specific URL
                 websocket_url_filter: str, # MS365 specific filter
                 user_input_selector: str,
                 submit_button_selector: str,
                 is_debug_logging: bool = False):
        super().__init__(edge_path, debug_profile_dir, debugging_port, is_debug_logging)
        self.copilot_url = copilot_url
        self.websocket_url_filter = websocket_url_filter # Used to identify the M365 chat WebSocket
        self.user_input_selector = user_input_selector
        self.submit_button_selector = submit_button_selector
        
        # M365 Copilot might establish a new WebSocket per prompt.
        # So, we might not store a single chat_websocket_request_id long-term here.
        # Instead, it might be captured and used within send_message_and_get_response.
        self.current_m365_chat_ws_request_id: typing.Optional[str] = None
        self.last_full_response_text: str = "" # To calculate diffs for streaming
        self.is_first_message_sent: bool = False # Flag to track if the first message has been sent

    async def connect(self) -> bool:
        logger.info("Connecting M365CopilotClient...")
        if not await self.connect_to_browser_and_page():
            logger.error("M365CopilotClient: Failed to connect to browser and page.")
            return False
        
        if not self.is_page_initialized:
            if not await self._navigate_and_initialize_cdp_domains(self.copilot_url, self.user_input_selector):
                logger.error(f"M365CopilotClient: Failed to navigate to {self.copilot_url}, initialize CDP domains, or find critical element '{self.user_input_selector}'.")
                return False
        
        logger.info("M365CopilotClient connected and page initialized successfully.")
        return True

    async def _capture_m365_chat_websocket_id(self) -> typing.Optional[str]:
        """
        Listens for Network.webSocketCreated events to capture the requestId 
        of the M365 chat WebSocket. This might be called per message.
        Returns the requestId if captured, None otherwise.
        """
        if not self.browser_cdp_ws or not self.page_cdp_session_id:
            logger.error("Cannot capture M365 chat WebSocket ID: Browser CDP WebSocket not connected or page not attached.")
            return None

        logger.info(f"Starting M365 chat WebSocket ID capture for filter: {self.websocket_url_filter}...")
        timeout_seconds = 20 # Timeout for capturing the WebSocket ID
        start_time = time.monotonic()
        
        captured_request_id: typing.Optional[str] = None

        try:
            while time.monotonic() - start_time < timeout_seconds:
                try:
                    message_str = await asyncio.wait_for(self.browser_cdp_ws.recv(), timeout=1.0)
                    data = json.loads(message_str)

                    if data.get("sessionId") == self.page_cdp_session_id and \
                       data.get("method") == "Network.webSocketCreated":
                        params = data.get("params", {})
                        ws_url = params.get("url")
                        # M365 WebSocket URLs can be dynamic, filter needs to be robust
                        if ws_url and self.websocket_url_filter in ws_url: # Use 'in' for partial match
                            captured_request_id = params.get("requestId")
                            logger.info(f"Captured M365 chat WebSocket ID: {captured_request_id} for URL: {ws_url}")
                            return captured_request_id
                except asyncio.TimeoutError:
                    pass 
                except json.JSONDecodeError:
                    logger.warning("Non-JSON message received during M365 WebSocket ID capture.")
                except Exception as e_inner:
                    logger.warning(f"Inner exception during M365 WebSocket ID capture: {e_inner}")
                    await asyncio.sleep(0.1)
            
            logger.warning(f"M365 chat WebSocket ID not found within {timeout_seconds}s for filter: {self.websocket_url_filter}")
            return None
        except asyncio.CancelledError:
            logger.info("M365 chat WebSocket ID capture task was cancelled.")
            raise
        except Exception as e:
            logger.exception(f"Exception in _capture_m365_chat_websocket_id: {e}")
            return None

    async def send_message_and_get_response(self, user_message: str) -> typing.AsyncGenerator[str, None]:
        if not self.is_page_initialized or not self.browser_cdp_ws or not self.page_cdp_session_id:
            logger.error("Cannot send message: M365 client not connected or page not initialized.")
            raise RuntimeError("M365 client not connected or page not initialized.")

        logger.info(f"M365CopilotClient preparing to send message: {self._format_prompt_for_log(user_message)}")
        self.last_full_response_text = "" # Reset for new message stream

        # UI Interactions (similar to StandardClient)
        root_node_id = await self._get_document_root_node_id()
        if root_node_id is None: raise RuntimeError("M365: Failed to get document root.")
        
        textarea_node_id = await self._query_selector_node_id(root_node_id, self.user_input_selector)
        if textarea_node_id is None: raise RuntimeError(f"M365: Textarea '{self.user_input_selector}' not found.")
        
        if not await self._focus_node_via_cdp(textarea_node_id):
            raise RuntimeError(f"M365: Failed to focus textarea '{self.user_input_selector}'.")
        if not await self._insert_text_via_cdp(user_message):
            raise RuntimeError("M365: Failed to insert text into textarea.")
        
        await asyncio.sleep(0.2)

        # Capture M365 chat WebSocket ID for this specific message
        logger.info("M365: Attempting to capture chat WebSocket ID for this message...")
        current_chat_ws_id = await self._capture_m365_chat_websocket_id()
        if not current_chat_ws_id:
            logger.error("M365: Failed to capture chat WebSocket ID for this message. Cannot proceed.")
            raise RuntimeError("M365: Failed to capture chat WebSocket ID for monitoring.")
        
        self.current_m365_chat_ws_request_id = current_chat_ws_id # Store it for this message cycle

        # Click submit
        logger.info(f"M365: Clicking submit button: '{self.submit_button_selector}'")
        if not await self._click_element_via_js(self.submit_button_selector):
            logger.warning(f"M365: Attempt to click submit button '{self.submit_button_selector}' failed or could not be confirmed.")
        await asyncio.sleep(0.5) # Allow time for click to initiate network activity

        # Monitor WebSocket frames
        logger.info(f"M365: Monitoring WebSocket frames for Request ID: {self.current_m365_chat_ws_request_id}...")
        
        if not self.browser_cdp_ws: raise RuntimeError("M365: Browser CDP WebSocket not available.")

        accumulated_payload = ""
        try:
            while True:
                message_str = await self.browser_cdp_ws.recv()
                data = json.loads(message_str)

                if data.get("sessionId") == self.page_cdp_session_id and \
                   data.get("method") == "Network.webSocketFrameReceived" and \
                   data.get("params", {}).get("requestId") == self.current_m365_chat_ws_request_id:
                    
                    payload_data_str = data["params"]['response']['payloadData']
                    if self.is_debug_logging:
                        logger.debug(f"M365 Raw WS payload for {self.current_m365_chat_ws_request_id}: {payload_data_str[:200]}...")
                    
                    accumulated_payload += payload_data_str
                    
                    # Process messages separated by RS character
                    while RS in accumulated_payload:
                        message_part, accumulated_payload = accumulated_payload.split(RS, 1)
                        if not message_part: continue

                        try:
                            payload_json = json.loads(message_part)
                            # MS365 Copilot returns the full message content each time.
                            # We need to extract the relevant text and then calculate the diff.
                            # The structure of `payload_json` needs to be determined from actual M365 traffic.
                            # Assuming a structure like: {"type": 1, "target": "update", "arguments": [{"messages": [{"text": "full new text"}]}]}
                            # Or: {"type": 2, "invocationId": "..."} followed by {"type": 3} for completion.

                            if payload_json.get("type") == 1 and payload_json.get("target") == "update":
                                current_full_text = None # Initialize to None
                                arguments = payload_json.get("arguments")
                                if isinstance(arguments, list) and len(arguments) > 0:
                                    messages_array = arguments[0].get("messages") # As per image, arguments is an array
                                    if isinstance(messages_array, list):
                                        # Iterate in reverse to find the latest bot message with text,
                                        # as sometimes there are multiple messages in the array.
                                        for msg_item in reversed(messages_array):
                                            if msg_item.get("author") == "bot" and "text" in msg_item:
                                                current_full_text = msg_item["text"]
                                                break
                                
                                if current_full_text is not None: # Ensure text was actually found
                                    diff_chunk = ""
                                    if not self.last_full_response_text: # First time receiving text
                                        diff_chunk = current_full_text
                                    elif current_full_text.startswith(self.last_full_response_text):
                                        diff_chunk = current_full_text[len(self.last_full_response_text):]
                                    elif self.last_full_response_text.startswith(current_full_text): # Text got shorter (e.g. backspace like behavior)
                                        logger.warning(f"M365: Text appears to have shortened. Old: '{self.last_full_response_text}', New: '{current_full_text}'. Sending empty chunk for now.")
                                        # This case is tricky for streaming diffs. For now, send nothing or handle as a replacement.
                                        # Sending an empty string means the client won't see the deletion unless it handles full state.
                                        # Alternatively, one could send a special signal or the new full text.
                                        # For simplicity, we'll send an empty diff, implying the client should update.
                                        # A better approach might be to send the full new text if a non-append change is detected.
                                        diff_chunk = "" # Or potentially current_full_text to force overwrite
                                    else: # Text changed in a way that's not a simple append or known prefix
                                        logger.warning(f"M365: Full text changed non-append/non-prefix. Old: '{self.last_full_response_text}', New: '{current_full_text}'. Sending new full text as diff.")
                                        # In this case, sending the new full text might be the safest to ensure client syncs.
                                        # However, for a pure "diff" stream, this is complex.
                                        # Let's send the part of current_full_text that doesn't overlap with the end of last_full_response_text
                                        # This is still a heuristic.
                                        # A simple approach: if it's not a prefix, send the whole new text.
                                        diff_chunk = current_full_text # Send the whole new text to ensure client gets the update
                                    
                                    if diff_chunk: # Only yield if there's something new to send
                                        yield diff_chunk
                                    self.last_full_response_text = current_full_text # Update last known full text
                            
                            # Check for completion message (type 2 followed by type 3, or just type 3)
                            # The image shows type 2 with "item" (user prompt echo) then type 3 for completion.
                            elif payload_json.get("type") == 2 and "item" in payload_json:
                                logger.debug(f"M365: Received invocation item (type 2): {payload_json.get('invocationId', '')} - {str(payload_json.get('item'))[:100]}")
                                # This is often an echo of the user's prompt or other intermediate data.
                                # Not typically part of the streamed response itself.
                            elif payload_json.get("type") == 3:
                                logger.info(f"M365: Response complete (type 3 message, invocationId: {payload_json.get('invocationId')}) for WS Request ID: {self.current_m365_chat_ws_request_id}.")
                                self.current_m365_chat_ws_request_id = None # WebSocket for this prompt is done
                                return
                            
                            # Handle other message types if necessary
                            # e.g., type 6 for telemetry, type 7 for stream close from server.
                            elif payload_json.get("type") == 2 and "item" in payload_json: # Invocation result
                                logger.debug(f"M365: Received invocation item (type 2): {payload_json.get('item')}")
                                # This might contain the final "user" message echo or other metadata.

                        except json.JSONDecodeError:
                            logger.warning(f"M365: Failed to decode JSON from WebSocket message part: {message_part[:200]}...")
                        except Exception as e_payload:
                            logger.exception(f"M365: Error processing WebSocket message part: {e_payload}")
                    
                    # If the payload_data_str did not end with RS, the remainder is in accumulated_payload
                    # and will be processed in the next iteration if more data arrives, or when the loop ends.

        except websockets.exceptions.ConnectionClosed as e_conn_closed:
            logger.warning(f"M365: Browser CDP WebSocket closed while monitoring chat frames: {e_conn_closed}")
            # For M365, if the specific chat WS closes, it might be normal after a response.
            # However, this is monitoring the main browser_cdp_ws. Its closure is an issue.
            self.current_m365_chat_ws_request_id = None
            raise RuntimeError(f"M365: Browser CDP WebSocket closed: {e_conn_closed}")
        except Exception as e_monitor:
            logger.exception(f"M365: Error during WebSocket monitoring for chat response: {e_monitor}")
            self.current_m365_chat_ws_request_id = None
            raise RuntimeError(f"M365: Error monitoring chat response: {e_monitor}")
        finally:
            # Reset request ID for this prompt as it's likely single-use for M365
            self.current_m365_chat_ws_request_id = None
            if not self.is_first_message_sent: # Set flag after the first successful message cycle
                # Check if any chunk was yielded to consider it successful
                # This is a bit indirect. A more direct success flag from the loop would be better.
                # For now, if we reached here without an exception that skipped the main loop,
                # assume it was a "successful" interaction in terms of sending/receiving.
                self.is_first_message_sent = True
            logger.debug("M365: send_message_and_get_response cycle finished.")

    async def reinitialize_page_session(self) -> bool:
        """
        Reinitializes the page session for M365CopilotClient.
        This involves:
        1. Resetting client-specific state (current_m365_chat_ws_request_id, last_full_response_text, is_first_message_sent).
        2. Resetting the base client's page initialization status.
        3. Re-navigating to the Copilot URL and re-initializing CDP domains.
        """
        logger.info("Reinitializing page session for M365CopilotClient...")

        # Reset client-specific state
        self.current_m365_chat_ws_request_id = None
        self.last_full_response_text = ""
        self.is_first_message_sent = False
        logger.debug("M365CopilotClient state reset (current_m365_chat_ws_request_id, last_full_response_text, is_first_message_sent).")

        # Reset base client's page initialization status
        self.is_page_initialized = False

        # Re-navigate and initialize CDP domains
        if not self.is_browser_cdp_connected or not self.page_cdp_session_id:
            logger.error("Cannot reinitialize page session: Browser CDP not connected or page not attached.")
            logger.info("Attempting full reconnect for M365 client...")
            return await self.connect()

        logger.info(f"M365: Re-navigating to {self.copilot_url} and re-initializing CDP domains...")
        if not await self._navigate_and_initialize_cdp_domains(self.copilot_url, self.user_input_selector):
            logger.error(f"M365: Failed to re-navigate to {self.copilot_url} or re-initialize CDP domains during session reinitialization.")
            return False

        logger.info("M365CopilotClient page session reinitialized successfully.")
        return True