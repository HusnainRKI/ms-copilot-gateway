# Edge Copilot WebSocket Monitor (Proof of Concept)

This Python script demonstrates how to:

1.  Launch Microsoft Edge with the remote debugging port enabled on Windows.
2.  Navigate to `https://copilot.microsoft.com/`.
3.  Use the Chrome DevTools Protocol (CDP) to:
    *   Attach to the Copilot page.
    *   Enable network monitoring.
    *   Simulate typing a test message into the chat input.
    *   Simulate clicking the send button.
    *   Monitor and print WebSocket messages exchanged with the Copilot backend (`wss://copilot.microsoft.com/c/api/chat?api-version=2`).

## Status

This is currently a proof-of-concept and under development. It provides a basic framework for interacting with Copilot programmatically via the debug protocol.

## Requirements

*   Python 3.x
*   `websockets` library (`pip install websockets`)
*   Microsoft Edge installed on Windows

## Usage

1.  Ensure the `EDGE_PATH` variable in `main.py` points to your `msedge.exe`.
2.  Run the script: `python main.py`
3.  The script will launch Edge, navigate to Copilot, send a test message, and start printing WebSocket events to the console.
4.  Press `Ctrl+C` in the console to stop the script and close Edge.