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
from contextlib import asynccontextmanager # Added for lifespan management

from fastapi import FastAPI, Request, HTTPException, status # Added status for clarity
from fastapi.responses import StreamingResponse, JSONResponse # Added JSONResponse
from fastapi.exceptions import RequestValidationError # To handle validation errors explicitly
from pydantic import BaseModel, Field # Added for request/response models
from typing import List, Optional, Union, Dict, Any # Added for type hinting

from copilot_client import CopilotClient # Import the new client

# Global CopilotClient instance
copilot_client_instance: Optional[CopilotClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global copilot_client_instance
    print("Initializing Copilot client...")
    copilot_client_instance = CopilotClient(
        edge_path=EDGE_PATH,
        debug_profile_dir=DEBUG_PROFILE_DIR,
        debugging_port=DEBUGGING_PORT,
        copilot_url=COPILOT_URL,
        websocket_url_filter=WEBSOCKET_URL_FILTER,
        user_input_selector=USER_INPUT_SELECTOR,
        submit_button_selector=SUBMIT_BUTTON_SELECTOR
    )
    if not await copilot_client_instance.connect():
        print("Failed to connect to Copilot during startup. Server might not function correctly.")
        # Optionally, raise an exception here to prevent server startup if connection is critical
    else:
        print("Copilot client connected successfully.")
    yield
    # --- Ensure cleanup happens ---
    print("Closing Copilot client (lifespan)...")
    if copilot_client_instance:
        await copilot_client_instance.close()
        print("Copilot client closed (lifespan).")
    else:
        print("Copilot client instance was None at shutdown (lifespan).")


app = FastAPI(lifespan=lifespan)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handles validation errors to provide more detailed logs."""
    print(f"Validation error for request: {request.method} {request.url}")
    print(f"Error details: {exc.errors()}")
    try:
        body = await request.json()
        print(f"Request body received: {body}")
    except Exception as e:
        print(f"Could not parse request body as JSON: {e}")
        try:
            raw_body = await request.body()
            print(f"Raw request body: {raw_body.decode(errors='ignore')}")
        except Exception as e_raw:
            print(f"Could not read raw request body: {e_raw}")

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
        async for chunk in copilot_client_instance.send_message_and_get_response(prompt):
            if first_chunk:
                # Send role for the first chunk if applicable (OpenAI does this)
                # However, Copilot client directly gives content.
                # We can simulate the role part if needed.
                # For now, just send content.
                # delta_role = ChatCompletionStreamChoiceDelta(role="assistant")
                # choice_role = ChatCompletionStreamChoice(delta=delta_role)
                # response_role = ChatCompletionStreamResponse(
                #     id=message_id_base,
                #     created=created_time,
                #     choices=[choice_role]
                # )
                # yield f"data: {response_role.model_dump_json()}\n\n"
                first_chunk = False

            delta = ChatCompletionStreamChoiceDelta(content=chunk)
            choice = ChatCompletionStreamChoice(delta=delta)
            response = ChatCompletionStreamResponse(
                id=message_id_base, # Each chunk can have the same base ID for the request
                created=created_time,
                choices=[choice]
            )
            yield f"data: {response.model_dump_json()}\n\n"

        # Send the final [DONE] message
        final_delta = ChatCompletionStreamChoiceDelta() # Empty content for final
        final_choice = ChatCompletionStreamChoice(delta=final_delta, finish_reason="stop")
        final_response = ChatCompletionStreamResponse(
            id=message_id_base,
            created=created_time,
            choices=[final_choice]
        )
        yield f"data: {final_response.model_dump_json()}\n\n"

    except Exception as e:
        print(f"Error during streaming: {e}")
        error_delta = ChatCompletionStreamChoiceDelta(content=f"Error during streaming: {str(e)}")
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
        print(f"Request successfully parsed. Model: {request_data.model}, Stream: {request_data.stream}, Messages: {request_data.messages}")
    except Exception as e:
        print(f"Error logging request body in chat_completions: {e}")
        # Fallback if .json() fails or if we want to avoid consuming body again
        # print(f"Request data (from Pydantic model): {request_data.model_dump_json()}")


    global copilot_client_instance
    if not copilot_client_instance or not copilot_client_instance.websocket_connection or not copilot_client_instance.session_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Copilot service not available or not connected.")

    # Extract the last user message as the prompt
    # Handle complex content field (string or list of text blocks)
    processed_prompt_str = ""
    user_message_to_process = None

    # Find the last user message
    for msg_idx in range(len(request_data.messages) - 1, -1, -1):
        if request_data.messages[msg_idx].role == "user":
            user_message_to_process = request_data.messages[msg_idx]
            break
    
    if not user_message_to_process:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No user message found in the request.")

    if isinstance(user_message_to_process.content, str):
        processed_prompt_str = user_message_to_process.content
    elif isinstance(user_message_to_process.content, list):
        # Concatenate text from all text blocks
        for block in user_message_to_process.content:
            if isinstance(block, TextContentBlock) and block.type == "text":
                processed_prompt_str += block.text + "\n"
            elif isinstance(block, dict) and block.get("type") == "text": # Handle if not fully parsed to TextContentBlock by Pydantic yet
                 processed_prompt_str += block.get("text","") + "\n"
        processed_prompt_str = processed_prompt_str.strip()
    else:
        # This case should ideally be caught by Pydantic validation if `content` is not str or List[TextContentBlock]
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid format for message content: {type(user_message_to_process.content)}")

    if not processed_prompt_str: # Check if after processing, the prompt is empty
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty prompt after processing user message content.")

    # Ensure prompt is a string before passing to other functions
    final_prompt: str = processed_prompt_str
    print(f"Processed prompt for Copilot: {final_prompt}")

    if request_data.stream:
        return StreamingResponse(stream_response_generator(final_prompt), media_type="text/event-stream")
    else:
        # Non-streaming response
        full_response_content = ""
        try:
            async for chunk in copilot_client_instance.send_message_and_get_response(final_prompt):
                full_response_content += chunk
            
            # For the response, content should be a simple string
            assistant_response_message = ChatMessage(role="assistant", content=full_response_content)
            choice = ChatCompletionChoice(
                message=assistant_response_message
            )
            return ChatCompletionResponse(choices=[choice], model=request_data.model)

        except Exception as e:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error processing non-streaming request: {str(e)}")

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
    print("\nCopilot REPL initialized (stdio mode). Type your message and press Enter.")
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
            if not client.websocket_connection or not client.session_id:
                print("Error: Copilot client is not connected. Cannot send message.")
                print("Attempting to reconnect client for REPL...")
                if await client.connect(): # Attempt to reconnect
                    print("Client reconnected. Please try your message again.")
                else:
                    print("Failed to reconnect client. Exiting REPL.")
                    break
                continue

            async for response_chunk in client.send_message_and_get_response(user_input):
                sys.stdout.write(response_chunk)
                sys.stdout.flush()
            sys.stdout.write("\n")
            sys.stdout.flush()

        except EOFError:
            print("\nEOF received, exiting REPL...")
            break
        except KeyboardInterrupt:
            print("\nREPL interrupted by user. Type 'exit' or 'quit' to close.")
            continue
        except Exception as e_repl:
            print(f"\nError in REPL loop: {e_repl}")
            import traceback
            traceback.print_exc()
            break

async def main():
    parser = argparse.ArgumentParser(description="Run Copilot interaction script either via stdio or as a ChatGPT-compatible server.")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run in stdin/stdout mode for direct command-line interaction.",
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host for the server (default: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=8000, help="Port for the server (default: 8000).")
    args = parser.parse_args()

    if args.stdio:
        # Stdio mode: Manually manage client lifecycle
        print("Initializing Copilot client for stdio mode...")
        # Note: copilot_client_instance is for FastAPI lifespan, use a local one here.
        stdio_client = CopilotClient(
            edge_path=EDGE_PATH,
            debug_profile_dir=DEBUG_PROFILE_DIR,
            debugging_port=DEBUGGING_PORT,
            copilot_url=COPILOT_URL,
            websocket_url_filter=WEBSOCKET_URL_FILTER,
            user_input_selector=USER_INPUT_SELECTOR,
            submit_button_selector=SUBMIT_BUTTON_SELECTOR
        )
        try:
            if await stdio_client.connect():
                print("Copilot client connected for stdio mode.")
                await main_stdio_repl(stdio_client)
            else:
                print("Failed to connect Copilot client for stdio mode. Exiting.")
        except KeyboardInterrupt:
            print("\nStdio mode interrupted by user.")
        except Exception as e_stdio_main:
            print(f"An unexpected error occurred in stdio mode: {e_stdio_main}")
            import traceback
            traceback.print_exc()
        finally:
            print("Cleaning up stdio mode client...")
            if stdio_client: # Ensure it was initialized
                await stdio_client.close()
            print("Stdio mode client cleanup complete.")
    else:
        # Server mode: FastAPI app with lifespan will handle client
        print(f"Starting ChatGPT-compatible server on http://{args.host}:{args.port}")
        # 'app' is defined globally with lifespan; uvicorn uses it.
        # The global 'copilot_client_instance' will be managed by 'lifespan'.
        try:
            config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
            server = uvicorn.Server(config)
            await server.serve()
        except KeyboardInterrupt:
            print("\nServer process interrupted by user. Lifespan exit handler should clean up.")
        except Exception as e_server_main:
            print(f"An unexpected error occurred while running the server: {e_server_main}")
            import traceback
            traceback.print_exc()
        # No explicit finally block needed here for client cleanup if lifespan handles it.
        # Uvicorn server.serve() is awaited, so this part is reached after server stops.
        print("Server has shut down.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nScript terminated by user (Ctrl+C at top level).")
    except Exception as e_global: # Catch any other unhandled exceptions
        print(f"Unhandled exception at top level: {e_global}")
        import traceback
        traceback.print_exc()
