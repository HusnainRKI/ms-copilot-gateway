# MS Copilot Gateway

This Python script provides two main functionalities for interacting with Microsoft Copilot:

1.  **ChatGPT-compatible API Server**: Launches an HTTP server (using FastAPI and Uvicorn) that exposes an OpenAI-compatible API endpoint (`/v1/chat/completions`). This allows AI editors and other tools that support the OpenAI API format to use Microsoft Copilot as a backend. It supports streaming responses.
2.  **Stdio REPL Mode**: Allows direct interaction with Copilot via a command-line REPL (Read-Eval-Print Loop).

The interaction with Copilot is achieved by:
*   Launching Microsoft Edge with the remote debugging port enabled on Windows.
*   Navigating to `https://copilot.microsoft.com/`.
*   Using the Chrome DevTools Protocol (CDP) to:
    *   Attach to the Copilot page.
    *   Enable network monitoring.
    *   Simulate typing the user's message into the chat input and clicking send.
    *   Monitor and stream WebSocket response messages.

## Status

This project provides a functional gateway to Copilot. Further enhancements and error handling are ongoing.
### Recent Improvements (May 2025)

*   **Improved multi-turn conversation stability**: The handling of the WebSocket `requestId` for chat messages has been refined. The `requestId` is now captured once during the first message exchange and reused for subsequent messages in the same session. This resolves issues where responses to second and later prompts were not being correctly processed by the script.
*   **Optimized page load wait time**: Reduced the timeout for waiting for the `Page.loadEventFired` event during initial browser connection to improve startup speed.

## Requirements

*   Python 3.10+ (as defined in `pyproject.toml`)
*   `uv` (for package management, optional but recommended)
*   Required Python libraries: `websockets`, `fastapi`, `uvicorn[standard]`
*   Microsoft Edge installed on Windows

You can install the dependencies using `uv` and the `pyproject.toml` file:
```bash
uv pip install -r requirements.txt
# Alternatively, if you have FastAPI and Uvicorn specified in pyproject.toml's dependencies:
# uv pip install fastapi "uvicorn[standard]" websockets
# (Assuming pyproject.toml is configured for uv, otherwise use pip directly with a requirements file or individual packages)
# For this project, the direct command is:
# uv pip install fastapi "uvicorn[standard]" websockets
```
(Note: Ensure `pyproject.toml` lists these dependencies if using `uv pip install .` or similar project-based installation with `uv`.)


## Usage

1.  Ensure the `EDGE_PATH` variable in `main.py` points to your `msedge.exe`.

### Server Mode (Default)

This mode starts an HTTP server compatible with the OpenAI Chat Completions API.

1.  Run the script:
    ```bash
    python main.py
    ```
2.  To specify a host and port (defaults to `0.0.0.0:8000`):
    ```bash
    python main.py --host 127.0.0.1 --port 8888
    ```
3.  To control how user messages from the client request are processed by the gateway before sending to Copilot, use the `--message-mode` option:
    *   `last` (default): Only the content of the last message with `role: "user"` is sent to Copilot.
    *   `all`: The content of all messages in the request are concatenated and sent to Copilot as a single prompt.
    Example:
    ```bash
    python main.py --message-mode all --port 8888
    ```
4.  The server will launch Edge, navigate to Copilot, and be ready to accept API requests.
5.  AI editors or clients can then be configured to use the endpoint: `http://<host>:<port>/v1/chat/completions`.

### Stdio REPL Mode

This mode allows direct command-line interaction with Copilot.

1.  Run the script with the `--stdio` flag:
    ```bash
    python main.py --stdio
    ```
2.  The script will launch Edge, navigate to Copilot, and then present a `>` prompt.
3.  Type your message and press Enter to send it to Copilot. The response will be streamed to the console.
4.  Type `exit` or `quit` (or press `Ctrl+D`) at the prompt to close the script and Edge. You can also use `Ctrl+C` to interrupt.