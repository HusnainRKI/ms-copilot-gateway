import asyncio
import json
import websockets # type: ignore
import time
import typing
import logging

from .base_client import BaseCopilotClient

logger = logging.getLogger("CopilotClient.Standard")

class StandardCopilotClient(BaseCopilotClient):
    def __init__(self,
                 edge_path: str,
                 debug_profile_dir: typing.Optional[str],
                 debugging_port: int,
                 copilot_url: str,
                 websocket_url_filter: str,
                 user_input_selector: str,
                 submit_button_selector: str,
                 is_debug_logging: bool = False):
        super().__init__(edge_path, debug_profile_dir, debugging_port, is_debug_logging)
        self.copilot_url = copilot_url
        self.websocket_url_filter = websocket_url_filter
        self.user_input_selector = user_input_selector
        self.submit_button_selector = submit_button_selector
        
        self.chat_websocket_request_id: typing.Optional[str] = None
        self.is_first_message_sent: bool = False # Tracks if the first message has been sent in this session

    async def connect(self) -> bool:
        logger.info("Connecting StandardCopilotClient...")
        if not await self.connect_to_browser_and_page():
            logger.error("StandardCopilotClient: Failed to connect to browser and page.")
            return False
        
        if not self.is_page_initialized: # Only navigate and init domains if not already done
            if not await self._navigate_and_initialize_cdp_domains(self.copilot_url, self.user_input_selector):
                logger.error(f"StandardCopilotClient: Failed to navigate to {self.copilot_url}, initialize CDP domains, or find critical element '{self.user_input_selector}'.")
                # Consider closing browser CDP connection if page init fails
                # await self.close_browser_cdp_ws() # Or full close if appropriate
                return False
        
        logger.info("StandardCopilotClient connected and page initialized successfully.")
        return True

    async def _capture_chat_websocket_id(self) -> bool:
        """
        Listens for Network.webSocketCreated events to capture the requestId 
        of the chat WebSocket for the standard Copilot.
        Returns True if ID was captured, False otherwise.
        """
        if not self.browser_cdp_ws or not self.page_cdp_session_id:
            logger.error("Cannot capture chat WebSocket ID: Browser CDP WebSocket not connected or page not attached.")
            return False

        logger.info(f"Starting chat WebSocket ID capture for filter: {self.websocket_url_filter}...")
        timeout_seconds = 20
        start_time = time.monotonic()

        try:
            while time.monotonic() - start_time < timeout_seconds:
                try:
                    message_str = await asyncio.wait_for(self.browser_cdp_ws.recv(), timeout=1.0)
                    data = json.loads(message_str)

                    if data.get("sessionId") == self.page_cdp_session_id and \
                       data.get("method") == "Network.webSocketCreated":
                        params = data.get("params", {})
                        ws_url = params.get("url")
                        if ws_url and ws_url.startswith(self.websocket_url_filter):
                            self.chat_websocket_request_id = params.get("requestId")
                            logger.info(f"Captured chat WebSocket ID: {self.chat_websocket_request_id} for URL: {ws_url}")
                            return True
                except asyncio.TimeoutError:
                    pass  # Expected during the wait loop
                except json.JSONDecodeError:
                    logger.warning("Non-JSON message received during WebSocket ID capture.")
                except Exception as e_inner:
                    logger.warning(f"Inner exception during WebSocket ID capture: {e_inner}")
                    await asyncio.sleep(0.1) # Brief pause before retrying recv
            
            logger.warning(f"Chat WebSocket ID not found within {timeout_seconds}s for filter: {self.websocket_url_filter}")
            return False
        except asyncio.CancelledError:
            logger.info("Chat WebSocket ID capture task was cancelled.")
            raise # Re-raise cancellation
        except Exception as e:
            logger.exception(f"Exception in _capture_chat_websocket_id: {e}")
            return False


    async def send_message_and_get_response(self, user_message: str) -> typing.AsyncGenerator[str, None]:
        if not self.is_page_initialized or not self.browser_cdp_ws or not self.page_cdp_session_id:
            logger.error("Cannot send message: Client not connected or page not initialized.")
            raise RuntimeError("Client not connected or page not initialized.")

        logger.info(f"StandardCopilotClient preparing to send message: {self._format_prompt_for_log(user_message)}")

        # 1. Get document root
        root_node_id = await self._get_document_root_node_id()
        if root_node_id is None:
            raise RuntimeError("Failed to get document root node ID.")

        # 2. Find textarea
        textarea_node_id = await self._query_selector_node_id(root_node_id, self.user_input_selector)
        if textarea_node_id is None:
            raise RuntimeError(f"Textarea '{self.user_input_selector}' not found.")

        # 3. Focus and insert text
        if not await self._focus_node_via_cdp(textarea_node_id):
            raise RuntimeError(f"Failed to focus textarea '{self.user_input_selector}'.")
        if not await self._insert_text_via_cdp(user_message): # Assumes focus is maintained
            raise RuntimeError("Failed to insert text into textarea.")
        
        logger.debug("Text input successful.")
        await asyncio.sleep(0.2) # Allow UI to process input if necessary

        # 4. Prepare to capture chat WebSocket ID if it's the first message or ID is missing.
        # For standard copilot, we capture it once and reuse.
        capture_task = None
        if self.chat_websocket_request_id is None:
            logger.info("Chat WebSocket ID is None. Starting WebSocket ID capture task...")
            # _capture_chat_websocket_id returns bool. The task will run this method.
            capture_task = asyncio.create_task(self._capture_chat_websocket_id())
        else:
            logger.info(f"Reusing existing chat WebSocket ID: {self.chat_websocket_request_id}")

        # 5. Click submit button
        logger.info(f"Clicking submit button: '{self.submit_button_selector}'")
        click_success = await self._click_element_via_js(self.submit_button_selector)
        
        if not click_success:
            if capture_task and not capture_task.done():
                logger.info("Click failed, cancelling WebSocket ID capture task.")
                capture_task.cancel()
            logger.error(f"Failed to click submit button '{self.submit_button_selector}'. Aborting message send.")
            raise RuntimeError(f"Failed to click submit button '{self.submit_button_selector}'.")
        
        await asyncio.sleep(0.5) # Allow time for click to initiate network activity, WS to be created.

        # 6. Await capture task if it was started
        if capture_task:
            logger.info("Waiting for WebSocket ID capture task to complete...")
            try:
                # The timeout for wait_for should be sufficient for the WS to be created and ID captured.
                # _capture_chat_websocket_id itself has an internal timeout for recv.
                capture_successful = await asyncio.wait_for(capture_task, timeout=25.0) # Increased timeout slightly
                if not capture_successful or self.chat_websocket_request_id is None:
                    logger.error("WebSocket ID capture task logic completed but ID is still None or capture indicated failure.")
                    raise RuntimeError("Failed to capture chat WebSocket ID for monitoring after click.")
            except asyncio.TimeoutError:
                logger.error("Timeout waiting for WebSocket ID capture task to complete after click.")
                if not capture_task.done(): # Ensure task is cancelled if timeout occurs here
                    capture_task.cancel()
                raise RuntimeError("Timeout waiting for chat WebSocket Request ID capture task.")
            except asyncio.CancelledError:
                logger.warning("WebSocket ID capture task was cancelled (possibly due to click failure or other issue).")
                # This might be redundant if click failure already raised, but good for other cancellation paths.
                raise RuntimeError("Chat WebSocket Request ID capture was cancelled.")
            except Exception as e_capture_wait:
                logger.exception(f"Error waiting for WebSocket ID capture task: {e_capture_wait}")
                if not capture_task.done():
                    capture_task.cancel()
                raise RuntimeError(f"Error during WebSocket ID capture: {str(e_capture_wait)}")
        
        if not self.chat_websocket_request_id: # Final check, crucial before monitoring
             logger.error("Chat WebSocket ID is still None after the entire capture process.")
             raise RuntimeError("Chat WebSocket ID is still None after capture process.")

        # 7. Monitor WebSocket frames for the response
        logger.info(f"Monitoring WebSocket frames for Request ID: {self.chat_websocket_request_id}...")
        
        if not self.browser_cdp_ws: # Should not happen if checks above passed
            raise RuntimeError("Browser CDP WebSocket is not available for monitoring.")

        try:
            while True:
                message_str = await self.browser_cdp_ws.recv()
                data = json.loads(message_str)

                if data.get("sessionId") == self.page_cdp_session_id and \
                   data.get("method") == "Network.webSocketFrameReceived" and \
                   data.get("params", {}).get("requestId") == self.chat_websocket_request_id:
                    
                    payload_data_str = data["params"]['response']['payloadData']
                    if self.is_debug_logging:
                        logger.debug(f"Raw WS payload for {self.chat_websocket_request_id}: {payload_data_str[:200]}...")
                    
                    # Standard Copilot sends JSON messages separated by a record separator (U+001E)
                    # However, the CDP event gives us one full message at a time.
                    # The payload_data_str itself is typically a single JSON object for standard Copilot.
                    try:
                        payload_json = json.loads(payload_data_str)
                        event_type = payload_json.get("event")

                        if event_type == "appendText":
                            text_chunk = payload_json.get("text", "")
                            yield text_chunk
                        elif event_type == "done":
                            logger.info(f"Response complete ('done' event) for {self.chat_websocket_request_id}.")
                            self.is_first_message_sent = True # Mark after successful message cycle
                            return
                        elif event_type:
                            # Log other known event types if necessary, or unexpected ones
                            logger.debug(f"[Received Event: {event_type}] Data: {payload_json}")
                        else:
                            # This case handles payloads that are valid JSON but don't have an "event" field
                            # or where "event" is null/empty.
                            # This might indicate a different message structure than expected.
                            # For standard Copilot, we primarily expect "appendText" or "done".
                            # Other structures might be telemetry or metadata.
                            # Example from original client: if "arguments" in payload_json ...
                            # This part can be expanded if other message types need specific handling.
                            if "type" in payload_json and "arguments" in payload_json:
                                # This was the tentative parsing in the previous version of this file.
                                # It might be for a different message format or an alternative way messages are sent.
                                # For now, we prioritize the "event" based parsing from the original client.
                                logger.debug(f"Received non-event based JSON payload: Type {payload_json.get('type')}, Args: {str(payload_json.get('arguments'))[:100]}...")
                            else:
                                logger.warning(f"Received JSON payload without a recognized 'event' or structure: {payload_data_str[:200]}...")
                                
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to decode JSON from WebSocket payload: {payload_data_str[:200]}...")
                    except Exception as e_payload:
                        logger.exception(f"Error processing WebSocket payload: {e_payload}")

        except websockets.exceptions.ConnectionClosed as e_conn_closed:
            logger.warning(f"Browser CDP WebSocket closed while monitoring chat frames: {e_conn_closed}")
            # Decide if this is an error or expected end of communication for this message
            # For standard copilot, the main CDP WS should stay open.
            # This might indicate an issue.
            raise RuntimeError(f"Browser CDP WebSocket closed: {e_conn_closed}")
        except Exception as e_monitor:
            logger.exception(f"Error during WebSocket monitoring for chat response: {e_monitor}")
            raise RuntimeError(f"Error monitoring chat response: {e_monitor}")
        finally:
            # For standard copilot, we typically mark first message sent after the first full exchange.
            # If the loop exits cleanly (e.g. "done" message), it's handled above.
            # If it exits due to an exception, is_first_message_sent might not be set,
            # which is okay as the exchange wasn't fully successful.
            pass