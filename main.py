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

from copilot_client import CopilotClient # Import the new client

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


async def main():
    parser = argparse.ArgumentParser(description="Run Copilot interaction script either via stdio or as a ChatGPT-compatible server.")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run in stdin/stdout mode for direct command-line interaction.",
    )
    # Add other arguments for server mode later, e.g., --host, --port
    args = parser.parse_args()

    client = CopilotClient(
        edge_path=EDGE_PATH,
        debug_profile_dir=DEBUG_PROFILE_DIR,
        debugging_port=DEBUGGING_PORT,
        copilot_url=COPILOT_URL,
        websocket_url_filter=WEBSOCKET_URL_FILTER,
        user_input_selector=USER_INPUT_SELECTOR,
        submit_button_selector=SUBMIT_BUTTON_SELECTOR
    )

    try:
        if not await client.connect():
            print("Failed to connect to Copilot. Exiting.")
            return

        if args.stdio:
            # --- REPL for interacting with Copilot (stdio mode) ---
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
                    async for response_chunk in client.send_message_and_get_response(user_input):
                        sys.stdout.write(response_chunk)
                        sys.stdout.flush()
                    sys.stdout.write("\n") # Add a newline after the full response
                    sys.stdout.flush()

                except EOFError:
                    print("\nEOF received, exiting REPL...")
                    break
                except KeyboardInterrupt:
                    print("\nREPL interrupted by user. Type 'exit' or 'quit' to close.")
                    # Allow loop to continue to prompt for exit or new command
                    continue
                except Exception as e_repl:
                    print(f"\nError in REPL loop: {e_repl}")
                    break # Exit REPL on other errors
        else:
            # --- ChatGPT-compatible server mode (default) ---
            print("Starting ChatGPT-compatible server mode...")
            # TODO: Implement server logic here
            # For now, we'll just keep the browser connection alive until interrupted
            print("Server mode placeholder. Press Ctrl+C to exit and close Edge.")
            try:
                while True:
                    await asyncio.sleep(1) # Keep alive
            except KeyboardInterrupt:
                print("\nServer mode interrupted by user.")
            except Exception as e_server:
                print(f"\nError in server mode placeholder: {e_server}")

    except KeyboardInterrupt:
        print("\nMain process interrupted by user.")
    except Exception as e:
        print(f"An unexpected error occurred in main: {e}")
        import traceback
        traceback.print_exc() # Detailed error display
    finally:
        print("Cleaning up...")
        if client: # Ensure client was initialized
            await client.close()
        print("Cleanup complete. Exiting.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # This handles Ctrl+C if it happens before or during asyncio.run setup
        print("\nScript terminated by user (Ctrl+C at top level).")
    except Exception as e:
        print(f"Unhandled exception at top level: {e}")
