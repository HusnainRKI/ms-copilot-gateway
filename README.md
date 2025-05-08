# Edge Copilot WebSocket Monitor (Proof of Concept)

This Python script demonstrates how to:

1.  Launch Microsoft Edge with the remote debugging port enabled on Windows.
2.  Navigate to `https://copilot.microsoft.com/`.
3.  Use the Chrome DevTools Protocol (CDP) to:
    *   Attach to the Copilot page.
    *   Enable network monitoring.
    *   Accept user messages via standard input in a REPL-style loop.
    *   Simulate typing the user's message into the chat input and clicking send.
    *   Monitor and stream WebSocket response messages to standard output.

## Status

This is currently a proof-of-concept and under development. It provides a basic framework for interacting with Copilot programmatically via the debug protocol.

## Requirements

*   Python 3.x
*   `websockets` library (`pip install websockets`)
*   Microsoft Edge installed on Windows

## Usage

1.  Ensure the `EDGE_PATH` variable in `main.py` points to your `msedge.exe`.
2.  Run the script: `python main.py`
3.  The script will launch Edge, navigate to Copilot, and then present a `>` prompt.
4.  Type your message and press Enter to send it to Copilot. The response will be streamed to the console.
5.  Type `exit` or `quit` (or press `Ctrl+D`) at the prompt to close the script and Edge. You can also use `Ctrl+C` to interrupt.