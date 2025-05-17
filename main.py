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

from copilot_client import CopilotClient # Import the new client

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
copilot_client_instance: Optional[CopilotClient] = None

# --- Application Settings ---
class AppSettings(BaseModel): # Using Pydantic for potential future validation/structure
    message_mode: str = "last" # Default value
    debug_logging: bool = False # Added for debug logging control

settings = AppSettings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global copilot_client_instance
    logger.info("Initializing Copilot client...")
    copilot_client_instance = CopilotClient(
        edge_path=EDGE_PATH,
        debug_profile_dir=DEBUG_PROFILE_DIR,
        debugging_port=DEBUGGING_PORT,
        copilot_url=COPILOT_URL,
        websocket_url_filter=WEBSOCKET_URL_FILTER,
        user_input_selector=USER_INPUT_SELECTOR,
        submit_button_selector=SUBMIT_BUTTON_SELECTOR,
        is_debug_logging=settings.debug_logging # Pass debug logging flag
    )
    if not await copilot_client_instance.connect():
        logger.error("Failed to connect to Copilot during startup. Server might not function correctly.")
        # Optionally, raise an exception here to prevent server startup if connection is critical
    else:
        logger.info("Copilot client connected successfully.")
    yield
    # --- Ensure cleanup happens ---
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
    if not copilot_client_instance or not copilot_client_instance.websocket_connection or not copilot_client_instance.session_id:
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


    global copilot_client_instance
    if not copilot_client_instance or not copilot_client_instance.websocket_connection or not copilot_client_instance.session_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Copilot service not available or not connected.")

    # Extract the last user message as the prompt
    # Handle complex content field (string or list of text blocks)
    processed_prompt_str = ""

    # Determine the actual processing mode based on settings and whether it's the first message
    actual_processing_mode = settings.message_mode
    if settings.message_mode == "all" and copilot_client_instance and copilot_client_instance.is_first_message_sent:
        logger.info("Message mode 'all' configured, but this is not the first message. Switching to 'last' mode for this request.")
        actual_processing_mode = "last"
    elif settings.message_mode == "all" and not (copilot_client_instance and copilot_client_instance.is_first_message_sent):
        logger.info("Message mode 'all' configured, and this is the first message. Using 'all' mode.")
    else: # settings.message_mode == "last"
        logger.info("Message mode 'last' configured. Using 'last' mode.")
        actual_processing_mode = "last"

    logger.info(f"Processing messages with actual mode: {actual_processing_mode}")

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

    # Ensure client is connected and page is initialized before sending message
    if not await copilot_client_instance.connect():
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to connect or initialize Copilot client.")

    if request_data.stream:
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
            return ChatCompletionResponse(choices=[choice], model=request_data.model)

        except RuntimeError as e_runtime: # Catch specific RuntimeError from CopilotClient
            logger.error(f"RuntimeError during non-streaming request from CopilotClient: {e_runtime}")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Error communicating with Copilot: {str(e_runtime)}")
        except Exception as e_general: # Catch any other unexpected errors
            logger.exception(f"Unexpected error during non-streaming request: {e_general}")
            # import traceback # No longer needed
            # traceback.print_exc()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An unexpected error occurred: {str(e_general)}")

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
# Selector modification may be required if Copilot's UI structure changes
USER_INPUT_SELECTOR = "textarea#userInput"
# Submit button selector (simple version)
SUBMIT_BUTTON_SELECTOR = 'button[data-testid="submit-button"]'


async def main_stdio_repl(client: CopilotClient):
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
            if not client.websocket_connection or not client.session_id:
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
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host for the server (default: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=8000, help="Port for the server (default: 8000).")
    parser.add_argument(
        "--message-mode",
        type=str,
        choices=["last", "all"],
        default="last", # Default is 'last'
        help="Defines how messages are processed: 'last' (only the last user message) or 'all' (all messages concatenated)."
    )
    parser.add_argument(
        "--debug-logging",
        action="store_true",
        help="Enable debug level logging and full prompt text logging."
    )
    args = parser.parse_args()

    # Setup logging as early as possible, using the debug_logging flag
    setup_logging(args.debug_logging)
    settings.debug_logging = args.debug_logging # Store for global access if needed by lifespan

    if args.stdio:
        # Stdio mode: Manually manage client lifecycle
        logger.info("Initializing Copilot client for stdio mode...")
        # Note: copilot_client_instance is for FastAPI lifespan, use a local one here.
        # stdio mode does not use the global 'settings.message_mode' from command line args for server.
        stdio_client = CopilotClient(
            edge_path=EDGE_PATH,
            debug_profile_dir=DEBUG_PROFILE_DIR,
            debugging_port=DEBUGGING_PORT,
            copilot_url=COPILOT_URL,
            websocket_url_filter=WEBSOCKET_URL_FILTER,
            user_input_selector=USER_INPUT_SELECTOR,
            submit_button_selector=SUBMIT_BUTTON_SELECTOR,
            is_debug_logging=args.debug_logging # Pass debug flag
        )
        try:
            if await stdio_client.connect():
                logger.info("Copilot client connected for stdio mode.")
                await main_stdio_repl(stdio_client) # Pass args for debug_logging if needed by REPL directly
            else:
                logger.error("Failed to connect Copilot client for stdio mode. Exiting.")
        except KeyboardInterrupt:
            logger.info("\nStdio mode interrupted by user.")
        except Exception as e_stdio_main:
            logger.exception(f"An unexpected error occurred in stdio mode: {e_stdio_main}")
        finally:
            logger.info("Cleaning up stdio mode client...")
            if stdio_client: # Ensure it was initialized
                await stdio_client.close()
            logger.info("Stdio mode client cleanup complete.")
    else:
        # Server mode: FastAPI app with lifespan will handle client
        settings.message_mode = args.message_mode # Set the global setting for server mode
        logger.info(f"Message processing mode set to: {settings.message_mode}")
        logger.info(f"Debug logging enabled: {settings.debug_logging}")
        logger.info(f"Starting ChatGPT-compatible server on http://{args.host}:{args.port}")
        # 'app' is defined globally with lifespan; uvicorn uses it.
        # The global 'copilot_client_instance' will be managed by 'lifespan'.
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
