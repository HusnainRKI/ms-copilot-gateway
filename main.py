import asyncio
import json
import subprocess
import websockets # Keep for type hints or direct use if any remains, though likely not
import time
import platform
import os
import sys
import tempfile
import urllib.request # Keep for potential direct use, though likely not
import urllib.error   # Keep for potential direct use, though likely not
import argparse
import uvicorn # Added for FastAPI server
import logging # Added for logging
import colorlog # Added for colored logging
from contextlib import asynccontextmanager # Added for lifespan management

from fastapi import FastAPI, Request, HTTPException, status # Added status for clarity
from fastapi.responses import StreamingResponse, JSONResponse # Added JSONResponse
from fastapi.exceptions import RequestValidationError # To handle validation errors explicitly
from pydantic import BaseModel, Field # Added for request/response models
from typing import List, Optional, Union, Dict, Any # Added for type hinting
import typing # Added to resolve Pylance error for typing.cast and typing.Union

# from copilot_client import CopilotClient # Old client, will be removed
from copilot_clients.base_client import BaseCopilotClient # For type hinting
from copilot_clients.client_factory import CopilotClientFactory
from config import settings # Import settings from config

# --- Logger Setup ---
logger = logging.getLogger("WebServer")

def setup_logging(debug_mode: bool = False):
    """Configures colored logging."""
    root_logger = logging.getLogger() # Get the root logger
    if debug_mode:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO
    
    # Set level for all loggers, including uvicorn, fastapi, etc.
    # We will set our specific logger level later if needed, but root sets the baseline
    root_logger.setLevel(log_level)

    handler = colorlog.StreamHandler()
    formatter = colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s - %(name)s - [%(levelname)s] - %(message)s%(reset)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red,bg_white',
        },
        secondary_log_colors={},
        style='%'
    )
    handler.setFormatter(formatter)
    handler.setLevel(log_level) # Ensure handler also respects the level

    # Clear existing handlers from the root logger to avoid duplicate messages
    # if this function is called multiple times or if basicConfig was called.
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # Set level for our specific application logger
    # This allows our logger to be more verbose if needed, while uvicorn might be less so.
    logging.getLogger("WebServer").setLevel(log_level)
    logging.getLogger("CopilotClient").setLevel(log_level) # Also set for client logger

def format_prompt_for_logging(prompt: str, is_debug: bool, max_len: int = 100) -> str:
    """Formats the prompt string for logging, showing total length and truncating if not in debug mode."""
    total_len = len(prompt)
    if is_debug or total_len <= max_len:
        # For full prompt or short prompts, show it as is, perhaps with length.
        # Replacing newlines might make it less readable if it's a multi-line prompt being shown fully.
        # However, for consistency with truncated version, we can replace newlines.
        # Or, decide based on 'is_debug' if newlines should be preserved.
        # For now, let's keep it simple and not replace newlines if showing full.
        if is_debug:
            return f"(len:{total_len}) '{prompt}'" # Show full prompt as is in debug
        else: # Short prompt, not in debug
             prompt_oneline = prompt.replace('\n', ' ')
             return f"(len:{total_len}) '{prompt_oneline}'" # Replace newlines for one-liner
    
    # Truncated prompt
    truncated_prompt = prompt[:max_len].replace('\n', ' ')
    return f"(len:{total_len}) '{truncated_prompt}...'"


# Global CopilotClient instance
copilot_client_instance: Optional[BaseCopilotClient] = None # Updated type hint

# AppSettings class and global settings instance are now imported from config.py

@asynccontextmanager
async def lifespan(app: FastAPI):
    global copilot_client_instance
    logger.info(f"Initializing Copilot client for type: {settings.copilot_type} via factory...")
    # active_copilot_config = settings.get_active_copilot_settings() # Factory handles this
    copilot_client_instance = CopilotClientFactory.create_client(settings)

    if not copilot_client_instance:
        logger.error(f"Failed to create Copilot client for type: {settings.copilot_type}. Server cannot start.")
        # Optionally raise an exception to prevent server startup
        # For now, we'll let it proceed, but connect() will likely fail or be None
        # This path should ideally prevent uvicorn from starting if client is critical.
        # However, lifespan manager might not have a direct way to stop uvicorn server start.
        # A more robust solution might involve a pre-startup check or a state variable.
        # For now, if create_client returns None, connect() will be called on None.
        # Let's add a check before calling connect.
        # yield will still happen, and then cleanup will try to close None.
        # This needs careful handling.
        # For now, let's assume create_client always returns a client or raises an error.
        # The factory returns Optional, so we must handle None.
        # If None, we cannot proceed with connect.
        # The server will start but API calls will fail.
        # This is not ideal. Let's log and the connect call will fail.
        # A better approach: raise an error in factory or here if client is None.
        # For now, the current structure will lead to an AttributeError if copilot_client_instance is None.
        # Let's ensure connect is only called if instance is not None.
        # And if it's None, the server effectively won't work.
        # This is a limitation of lifespan not being able to easily abort server start.
        # Let's assume the factory logs an error and returns None.
        # The `connect` call below will then fail if instance is None.
        # This is acceptable for now.
    elif not await copilot_client_instance.connect():
        logger.error("Failed to connect to Copilot during startup. Server might not function correctly.")
    else:
        logger.info("Copilot client connected successfully.")
    yield
    logger.info("Closing Copilot client (lifespan)...")
    if copilot_client_instance:
        await copilot_client_instance.close()
        logger.info("Copilot client closed (lifespan).")
    else:
        logger.warning("Copilot client instance was None at shutdown (lifespan).")


app = FastAPI(lifespan=lifespan)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handles validation errors to provide more detailed logs."""
    logger.error(f"Validation error for request: {request.method} {request.url}")
    logger.error(f"Error details: {exc.errors()}")
    try:
        body = await request.json()
        logger.debug(f"Request body received: {body}")
    except Exception as e:
        logger.error(f"Could not parse request body as JSON: {e}")
        try:
            raw_body = await request.body()
            logger.debug(f"Raw request body: {raw_body.decode(errors='ignore')}")
        except Exception as e_raw:
            logger.error(f"Could not read raw request body: {e_raw}")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors(), "body_received_for_debug": "see server logs"},
    )

# --- Request and Response Models for OpenAI Compatibility ---
class TextContentBlock(BaseModel):
    type: str
    text: str

class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[TextContentBlock]] # Allow string or list of text content blocks

class ChatCompletionRequest(BaseModel):
    model: str = "copilot" # Model can be fixed or configurable
    messages: List[ChatMessage]
    stream: bool = False
    # Add other common parameters if needed, e.g., temperature, max_tokens, etc.
    # For now, we'll keep it simple and primarily use the last user message.

last_final_chat_message: Optional[ChatMessage] = None # Stores the last ChatMessage from the last successful request's messages list

# For streaming responses
class ChatCompletionStreamChoiceDelta(BaseModel):
    content: Optional[str] = None
    role: Optional[str] = None

class ChatCompletionStreamChoice(BaseModel):
    delta: ChatCompletionStreamChoiceDelta
    finish_reason: Optional[str] = None
    index: int = 0

class ChatCompletionStreamResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{time.time_ns()}")
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "copilot" # Should match the request or actual model used
    choices: List[ChatCompletionStreamChoice]

# For non-streaming responses (currently not the primary focus but good for completeness)
class ChatCompletionChoice(BaseModel):
    message: ChatMessage
    finish_reason: Optional[str] = "stop"
    index: int = 0

class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{time.time_ns()}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "copilot"
    choices: List[ChatCompletionChoice]
    # usage: Optional[UsageInfo] = None # Placeholder for token usage if implemented

async def stream_response_generator(prompt: str):
    """
    Generates streaming responses from the Copilot client.
    Yields data in the Server-Sent Events (SSE) format required by OpenAI API.
    """
    global copilot_client_instance
    # Updated attribute names: websocket_connection -> browser_cdp_ws, session_id -> page_cdp_session_id
    # Also check if the client instance itself exists
    if not copilot_client_instance or \
       not copilot_client_instance.is_browser_cdp_connected or \
       not copilot_client_instance.page_cdp_session_id:
        # This should ideally be caught before starting the stream,
        # but as a fallback:
        error_response = ChatCompletionStreamResponse(
            choices=[ChatCompletionStreamChoice(
                delta=ChatCompletionStreamChoiceDelta(content="Error: Copilot client not connected or initialized."),
                finish_reason="error"
            )]
        )
        yield f"data: {error_response.model_dump_json()}\n\n"
        yield "data: [DONE]\n\n"
        return

    message_id_base = f"chatcmpl-{time.time_ns()}"
    created_time = int(time.time())

    try:
        first_chunk = True
        # Attempt to get response from Copilot client
        async for chunk in copilot_client_instance.send_message_and_get_response(prompt):
            if first_chunk:
                first_chunk = False
            delta = ChatCompletionStreamChoiceDelta(content=chunk)
            choice = ChatCompletionStreamChoice(delta=delta)
            response = ChatCompletionStreamResponse(
                id=message_id_base,
                created=created_time,
                choices=[choice]
            )
            yield f"data: {response.model_dump_json()}\n\n"

        # If the loop completes without error, send a normal stop
        final_delta = ChatCompletionStreamChoiceDelta()
        final_choice = ChatCompletionStreamChoice(delta=final_delta, finish_reason="stop")
        final_response = ChatCompletionStreamResponse(
            id=message_id_base,
            created=created_time,
            choices=[final_choice]
        )
        yield f"data: {final_response.model_dump_json()}\n\n"

    except RuntimeError as e_runtime: # Catch specific RuntimeError from CopilotClient
        logger.error(f"RuntimeError during streaming from CopilotClient: {e_runtime}")
        error_delta = ChatCompletionStreamChoiceDelta(content=f"Error communicating with Copilot: {str(e_runtime)}")
        error_choice = ChatCompletionStreamChoice(delta=error_delta, finish_reason="error")
        error_response_obj = ChatCompletionStreamResponse(
            id=message_id_base,
            created=created_time,
            choices=[error_choice]
        )
        yield f"data: {error_response_obj.model_dump_json()}\n\n"
    except Exception as e_general: # Catch any other unexpected errors
        logger.exception(f"Unexpected error during streaming: {e_general}")
        # import traceback # No longer needed, logger.exception handles it
        # traceback.print_exc()
        error_delta = ChatCompletionStreamChoiceDelta(content=f"An unexpected error occurred: {str(e_general)}")
        error_choice = ChatCompletionStreamChoice(delta=error_delta, finish_reason="error")
        error_response_obj = ChatCompletionStreamResponse(
            id=message_id_base,
            created=created_time,
            choices=[error_choice]
        )
        yield f"data: {error_response_obj.model_dump_json()}\n\n"
    finally:
        yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request_data: ChatCompletionRequest, raw_request: Request): # Changed to request_data to avoid clash, added raw_request
    # Log the received request for debugging
    try:
        # request_body_for_log = await raw_request.json() # This consumes the body, be careful
        # print(f"Received /v1/chat/completions request: {request_body_for_log}")
        # Pydantic model 'request_data' already contains the parsed body if validation passed up to this point.
        # If we are here, basic Pydantic validation passed.
        # However, the custom exception handler above will catch Pydantic errors.
        logger.info(f"Request successfully parsed. Model: {request_data.model}, Stream: {request_data.stream}, Messages count: {len(request_data.messages)}")
        if settings.debug_logging: # Log full messages only in debug mode
            logger.debug(f"Full messages: {request_data.messages}")
    except Exception as e:
        logger.exception(f"Error logging request body in chat_completions: {e}")
        # Fallback if .json() fails or if we want to avoid consuming body again
        # logger.debug(f"Request data (from Pydantic model): {request_data.model_dump_json()}")


    global copilot_client_instance, last_final_chat_message
    # Updated attribute names and check
    if not copilot_client_instance or \
       not copilot_client_instance.is_browser_cdp_connected or \
       not copilot_client_instance.page_cdp_session_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Copilot service not available or not connected.")

    # Extract the last user message as the prompt
    # Handle complex content field (string or list of text blocks)
    processed_prompt_str = ""

    # Determine the actual processing mode based on settings and whether it's the first message.
    # Determine actual_processing_mode based on client's is_first_message_sent state
    # This ensures that for the first message of any session (including after reinitialization),
    # we process all messages, and for subsequent messages, only the last.
    client_is_definitely_not_on_first_message = False
    if copilot_client_instance:
        from copilot_clients.standard_client import StandardCopilotClient # Local import
        from copilot_clients.m365_client import M365CopilotClient # Local import
        if isinstance(copilot_client_instance, (StandardCopilotClient, M365CopilotClient)):
            if copilot_client_instance.is_first_message_sent: # True means it's NOT the first message
                client_is_definitely_not_on_first_message = True
            if settings.debug_logging:
                 logger.debug(f"Client state for processing mode decision: copilot_client_instance.is_first_message_sent = {copilot_client_instance.is_first_message_sent}")
        elif settings.debug_logging:
            logger.debug("Client instance is not StandardCopilotClient or M365CopilotClient, is_first_message_sent state not applicable for mode decision here.")
    elif settings.debug_logging:
        logger.debug("Copilot client instance is None, cannot determine is_first_message_sent.")

    if client_is_definitely_not_on_first_message:
        actual_processing_mode = "last"
        logger.info("Processing mode: 'last' (subsequent message in an ongoing session).")
    else:
        actual_processing_mode = "all"
        logger.info("Processing mode: 'all' (first message of a session, or client state indicates first message).")

    if actual_processing_mode == "last":
        user_message_to_process = None
        for message in reversed(request_data.messages): # Iterate from the end
            if message.role == "user":
                user_message_to_process = message
                break
        
        if not user_message_to_process:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No user message found in 'last' mode.")

        # Process content of the found user message
        if isinstance(user_message_to_process.content, str):
            processed_prompt_str = user_message_to_process.content.strip()
        elif isinstance(user_message_to_process.content, list):
            temp_content_list = []
            for block in user_message_to_process.content:
                if isinstance(block, TextContentBlock) and block.type == "text":
                    temp_content_list.append(block.text)
                elif isinstance(block, dict) and block.get("type") == "text": # Handle if not fully parsed
                    temp_content_list.append(block.get("text", ""))
            processed_prompt_str = "\n".join(temp_content_list).strip()
        # processed_prompt_str could be empty if content was empty or only whitespace.
        # The generic check for empty prompt later will catch this.

    elif actual_processing_mode == "all":
        # Concatenate all messages with role prefixes
        messages_with_roles = []
        for message in request_data.messages:
            current_content_str = ""
            if isinstance(message.content, str):
                current_content_str = message.content.strip()
            elif isinstance(message.content, list):
                temp_content_list = []
                for block in message.content:
                    if isinstance(block, TextContentBlock) and block.type == "text":
                        temp_content_list.append(block.text.strip())
                    elif isinstance(block, dict) and block.get("type") == "text": # Handle if not fully parsed
                        temp_content_list.append(block.get("text", "").strip())
                current_content_str = "\n".join(temp_content_list).strip()

            if current_content_str: # Add non-empty content with role prefix
                # Use simple prefixes for roles
                role_prefix = ""
                if message.role == "system":
                    role_prefix = "System: "
                elif message.role == "user":
                    role_prefix = "User: "
                elif message.role == "assistant":
                    role_prefix = "Assistant: "
                # Other roles (like 'tool') might be ignored or handled differently if needed

                messages_with_roles.append(f"{role_prefix}{current_content_str}")

        processed_prompt_str = "\n\n".join(messages_with_roles) # Use double newline between messages

    if not processed_prompt_str: # Check if after processing, the prompt is empty
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty prompt after processing all message contents.")

    # Ensure prompt is a string before passing to other functions
    final_prompt: str = processed_prompt_str
    logger.info(f"Processed prompt for Copilot: {format_prompt_for_logging(final_prompt, settings.debug_logging)}")

    # --- Session Reinitialization Logic based on ChatMessage history ---
    is_new_session = True # Assume new session by default
    if not request_data.messages:
        logger.warning("Request data messages list is empty. Defaulting to new session.")
    elif last_final_chat_message is None:
        logger.info("No previous chat message stored. Treating as new session (or first request).")
    else:
        # Check Pattern 1: [..., LFC, Assistant, User_current]
        # LFC is last_final_chat_message (which was messages[-1] of previous request)
        if len(request_data.messages) >= 3:
            if request_data.messages[-3] == last_final_chat_message:
                logger.info("Session continued (Pattern 1 detected): Message at index -3 matches previous request's last message.")
                is_new_session = False
            elif settings.debug_logging and is_new_session: # Only log if still considered new and Pattern 1 was possible
                logger.debug(f"Pattern 1 Check (len >= 3): messages[-3] ({request_data.messages[-3]}) != LFC ({last_final_chat_message})")

        # If not Pattern 1, or if len < 3, check Pattern 2: [..., LFC, User_current]
        if is_new_session and len(request_data.messages) >= 2: # is_new_session is still true here if Pattern 1 didn't match
            if request_data.messages[-2] == last_final_chat_message:
                logger.info("Session continued (Pattern 2 detected): Message at index -2 matches previous request's last message.")
                is_new_session = False
            elif settings.debug_logging and is_new_session: # Only log if still considered new and Pattern 2 was possible
                 logger.debug(f"Pattern 2 Check (len >= 2): messages[-2] ({request_data.messages[-2]}) != LFC ({last_final_chat_message})")

        if is_new_session: # If neither pattern matched
            logger.info("New session determined: Current messages do not form a recognized continuation pattern from the previous last message.")
            if settings.debug_logging:
                logger.debug(f"LFC was: {last_final_chat_message}")
                logger.debug(f"Current messages: {request_data.messages}")
    
    # If after all checks, is_new_session is still True, it means it's genuinely a new session or an unmatchable pattern.

    if is_new_session:
        if copilot_client_instance and hasattr(copilot_client_instance, 'reinitialize_page_session'):
            logger.info("Attempting to reinitialize Copilot page session.")
            if not await copilot_client_instance.reinitialize_page_session():
                logger.error("Failed to reinitialize Copilot page session. Service might be unavailable.")
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to reinitialize Copilot page session.")
            logger.info("Copilot page session reinitialized successfully.")
            # This is now the "first message" of a new session.
            # Force actual_processing_mode to "all" and reconstruct final_prompt.
            actual_processing_mode = "all"
            logger.info(f"Processing mode for reinitialized session explicitly set to: {actual_processing_mode}")

            # Reconstruct final_prompt using "all" messages from request_data
            messages_for_prompt_reconstruction = []
            for message_item in request_data.messages: # Iterate through original request messages
                current_content_str_rebuild = ""
                if isinstance(message_item.content, str):
                    current_content_str_rebuild = message_item.content.strip()
                elif isinstance(message_item.content, list):
                    temp_content_list_rebuild = []
                    for block_content in message_item.content: # Renamed 'block' to avoid conflict
                        if isinstance(block_content, TextContentBlock) and block_content.type == "text":
                            temp_content_list_rebuild.append(block_content.text.strip())
                        elif isinstance(block_content, dict) and block_content.get("type") == "text":
                            temp_content_list_rebuild.append(block_content.get("text", "").strip())
                    current_content_str_rebuild = "\n".join(temp_content_list_rebuild).strip()

                if current_content_str_rebuild:
                    role_prefix_rebuild = ""
                    if message_item.role == "system": role_prefix_rebuild = "System: "
                    elif message_item.role == "user": role_prefix_rebuild = "User: "
                    elif message_item.role == "assistant": role_prefix_rebuild = "Assistant: "
                    messages_for_prompt_reconstruction.append(f"{role_prefix_rebuild}{current_content_str_rebuild}")
            
            reconstructed_final_prompt = "\n\n".join(messages_for_prompt_reconstruction)
            
            if not reconstructed_final_prompt:
                logger.warning("Reconstructed prompt for reinitialized 'all' mode is empty. This is unexpected. Original final_prompt will be used if not empty, or error may occur.")
                # If original final_prompt was also empty, the check at line ~412 (original numbering) will catch it.
            else:
                final_prompt = reconstructed_final_prompt # IMPORTANT: Update the final_prompt to be sent
                logger.info(f"Reconstructed prompt for Copilot (after reinit): {format_prompt_for_logging(final_prompt, settings.debug_logging)}")

        elif copilot_client_instance:
             logger.warning("Copilot client instance does not support reinitialize_page_session. Proceeding with current session state.")
        else:
            logger.error("Copilot client instance is None. Cannot reinitialize session.")
            # This should ideally be caught earlier by the instance check.
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Copilot client instance is None.")
    # --- End Session Reinitialization Logic ---

    # Ensure client is connected and page is initialized before sending message
    # The connect() call here ensures that if the client wasn't connected at all (e.g. first request, or after a full close),
    # it attempts to connect. If reinitialize_page_session was called, it should have handled re-navigation.
    # If reinitialize_page_session was not supported or failed, connect() might try to establish a fresh connection
    # or re-establish a lost one.
    if not await copilot_client_instance.connect(): # This connect() might be redundant if reinitialize_page_session succeeded and did its own navigation.
                                                 # However, it's a good fallback if reinitialization wasn't needed or failed partially.
                                                 # BaseClient.connect() itself checks if already connected.
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to connect or initialize Copilot client.")

    if request_data.stream:
        # Successfully processed up to this point, update last_final_chat_message
        if request_data.messages:
            last_final_chat_message = request_data.messages[-1]
        else: # Should not happen if validation passed
            last_final_chat_message = None
        return StreamingResponse(stream_response_generator(final_prompt), media_type="text/event-stream")
    else:
        # Non-streaming response
        full_response_content = ""
        try:
            async for chunk in copilot_client_instance.send_message_and_get_response(final_prompt):
                full_response_content += chunk
            
            if not full_response_content and copilot_client_instance: # Check if content is empty and client exists
                 # This might indicate an issue if send_message_and_get_response yielded nothing
                 # but didn't raise an exception handled below.
                 logger.warning("Non-streaming response from Copilot was empty.")
                 # Depending on desired behavior, could raise HTTPException here or return empty content.

            assistant_response_message = ChatMessage(role="assistant", content=full_response_content)
            choice = ChatCompletionChoice(
                message=assistant_response_message
            )
            # Successfully processed up to this point, update last_final_chat_message
            if request_data.messages:
                last_final_chat_message = request_data.messages[-1]
            else: # Should not happen if validation passed
                last_final_chat_message = None
            return ChatCompletionResponse(choices=[choice], model=request_data.model)

        except RuntimeError as e_runtime: # Catch specific RuntimeError from CopilotClient
            logger.error(f"RuntimeError during non-streaming request from CopilotClient: {e_runtime}")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Error communicating with Copilot: {str(e_runtime)}")
        except Exception as e_general: # Catch any other unexpected errors
            logger.exception(f"Unexpected error during non-streaming request: {e_general}")
            # import traceback # No longer needed
            # traceback.print_exc()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An unexpected error occurred: {str(e_general)}")

# Settings constants are now in config.py and accessed via the settings object.


async def main_stdio_repl(client: BaseCopilotClient): # Updated type hint
    """Handles the REPL interaction when in stdio mode."""
    logger.info("\nCopilot REPL initialized (stdio mode). Type your message and press Enter.")
    logger.info("Type 'exit' or 'quit' or press Ctrl+D (EOF) to terminate.")
    while True:
        try:
            sys.stdout.write("> ")
            sys.stdout.flush()
            user_input = sys.stdin.readline().strip()

            if not user_input or user_input.lower() in ["exit", "quit"]:
                logger.info("\nExiting REPL...")
                break

            logger.info(f"Sending to Copilot: {format_prompt_for_logging(user_input, settings.debug_logging)}") # Use settings for debug_logging
            # Updated attribute names
            if not client.is_browser_cdp_connected or not client.page_cdp_session_id:
                logger.error("Copilot client is not connected. Cannot send message.")
                logger.info("Attempting to reconnect client for REPL...")
                if await client.connect(): # Attempt to reconnect
                    logger.info("Client reconnected. Please try your message again.")
                else:
                    logger.error("Failed to reconnect client. Exiting REPL.")
                    break
                continue

            async for response_chunk in client.send_message_and_get_response(user_input):
                sys.stdout.write(response_chunk)
                sys.stdout.flush()
            sys.stdout.write("\n") # Ensure a newline after the full response
            sys.stdout.flush()

        except EOFError:
            logger.info("\nEOF received, exiting REPL...")
            break
        except KeyboardInterrupt:
            logger.info("\nREPL interrupted by user. Type 'exit' or 'quit' to close.")
            continue # Allow user to continue or exit cleanly
        except Exception as e_repl:
            logger.exception(f"\nError in REPL loop: {e_repl}")
            break # Exit on other errors

async def main():
    parser = argparse.ArgumentParser(description="Run Copilot interaction script either via stdio or as a ChatGPT-compatible server.")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run in stdin/stdout mode for direct command-line interaction.",
    )
    parser.add_argument("--host", type=str, default=settings.host, help="Host for the server.")
    parser.add_argument("--port", type=int, default=settings.port, help="Port for the server.")
    parser.add_argument(
        "--debug-logging",
        action="store_true", # Action 'store_true' implies default is False if not specified.
                              # If we want the default from settings to be True if settings.debug_logging is True,
                              # we might need to handle it post-parsing or set 'default' carefully if the action wasn't 'store_true'.
                              # For 'store_true', if the flag is present, it's True, otherwise False.
                              # We will update settings.debug_logging based on args.debug_logging.
        help="Enable debug level logging and full prompt text logging. Overrides default from config if specified."
    )
    parser.add_argument(
        "--copilot-type",
        type=str,
        choices=["standard", "m365"],
        default=settings.copilot_type,
        help="Specify the Copilot type to use: 'standard' or 'm365'."
    )
    args = parser.parse_args()

    # Update settings from command line arguments
    # For 'store_true' flags like debug_logging, args.debug_logging will be True if flag is present, False otherwise.
    # If the flag is present, it overrides the default from settings. If not present, we keep the settings default.
    # However, the typical behavior of argparse is that args.debug_logging will BE the value (True if passed, False if not, if default was False).
    # If settings.debug_logging was True, and --debug-logging is NOT passed, args.debug_logging would be False.
    # To ensure CLI can override, but config default is used if CLI flag absent:
    if args.debug_logging is not None and args.debug_logging != settings.debug_logging: # Check if CLI provided a value different from settings default
         settings.debug_logging = args.debug_logging
    # For other args, the default from settings is used if not provided on CLI.
    settings.host = args.host
    settings.port = args.port
    settings.copilot_type = args.copilot_type


    # Setup logging as early as possible, using the debug_logging flag from settings
    setup_logging(settings.debug_logging)

    if args.stdio:
        logger.info(f"Initializing Copilot client for stdio mode (type: {settings.copilot_type}) via factory...")
        # active_copilot_config = settings.get_active_copilot_settings() # Factory handles this
        stdio_client: Optional[BaseCopilotClient] = CopilotClientFactory.create_client(settings)

        if not stdio_client:
            logger.error(f"Failed to create Copilot client for stdio mode (type: {settings.copilot_type}). Exiting.")
            return # Exit if client creation failed

        try:
            if await stdio_client.connect():
                logger.info("Copilot client connected for stdio mode.")
                await main_stdio_repl(stdio_client)
            else:
                logger.error("Failed to connect Copilot client for stdio mode. Exiting.")
        except KeyboardInterrupt:
            logger.info("\nStdio mode interrupted by user.")
        except Exception as e_stdio_main:
            logger.exception(f"An unexpected error occurred in stdio mode: {e_stdio_main}")
        finally:
            logger.info("Cleaning up stdio mode client...")
            if stdio_client:
                await stdio_client.close()
            logger.info("Stdio mode client cleanup complete.")
    else:
        # Server mode: FastAPI app with lifespan will handle client
        logger.info(f"Debug logging enabled: {settings.debug_logging}")
        logger.info(f"Copilot type selected: {settings.copilot_type}")
        logger.info(f"Starting ChatGPT-compatible server on http://{settings.host}:{settings.port}")
        try:
            # Uvicorn's log_level will be overridden by our root logger setup if it's more verbose.
            # If our root logger is INFO, and uvicorn's is DEBUG, uvicorn will still log DEBUG.
            # To control uvicorn's logging level strictly, its own logger needs to be configured.
            # For now, our setup_logging will make our app logs colored and respect debug_logging.
            # Uvicorn's default colored logs will still appear for its own messages.
            config = uvicorn.Config(app, host=args.host, port=args.port, log_config=None) # Pass log_config=None to prevent uvicorn from overriding our setup
            server = uvicorn.Server(config)
            await server.serve()
        except KeyboardInterrupt:
            logger.info("\nServer process interrupted by user. Lifespan exit handler should clean up.")
        except Exception as e_server_main:
            logger.exception(f"An unexpected error occurred while running the server: {e_server_main}")
        # No explicit finally block needed here for client cleanup if lifespan handles it.
        # Uvicorn server.serve() is awaited, so this part is reached after server stops.
        logger.info("Server has shut down.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # This might be redundant if logger is already set up and main() handles it.
        # However, if main() itself fails before logging is set up, this can be a fallback.
        if logging.getLogger().hasHandlers():
            logger.info("\nScript terminated by user (Ctrl+C at top level).")
        else:
            print("\nScript terminated by user (Ctrl+C at top level - pre-logging).")
    except Exception as e_global: # Catch any other unhandled exceptions
        if logging.getLogger().hasHandlers():
            logger.exception(f"Unhandled exception at top level: {e_global}")
        else:
            print(f"Unhandled exception at top level (pre-logging): {e_global}")
            import traceback
            traceback.print_exc()
