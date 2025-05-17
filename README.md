# MS Copilot Gateway (Experimental)

> **Note:** This is an experimental project. See "Important Considerations" below.

This Python script provides two main functionalities for interacting with Microsoft Copilot.

The general architecture is as follows:

```mermaid
graph LR
    A[Your AI Editor / Client] -- OpenAI API Request --> B(MS Copilot Gateway);
    B -- CDP Commands --> C(Microsoft Edge);
    C -- User Input & WebSocket --> D(Microsoft Copilot Website);
    D -- WebSocket Response --> C;
    C -- CDP Events --> B;
    B -- OpenAI API Response --> A;
```

**Functionalities:**

1.  **ChatGPT-compatible API Server**: Launches an HTTP server (using FastAPI and Uvicorn) that exposes an OpenAI-compatible API endpoint (`/v1/chat/completions`). This allows AI editors and other tools that support the OpenAI API format to use Microsoft Copilot as a backend. It supports streaming responses.
2.  **Stdio REPL Mode**: Allows direct interaction with Copilot via a command-line REPL (Read-Eval-Print Loop).

The interaction with Copilot is achieved by:
*   Launching Microsoft Edge with the remote debugging port enabled on Windows.
*   Navigating to the selected Copilot URL (either `https://copilot.microsoft.com/` for standard Copilot or `https://m365.cloud.microsoft/chat` for MS365 Copilot, configurable via a command-line argument).
*   Using the Chrome DevTools Protocol (CDP) to:
    *   Attach to the Copilot page.
    *   Enable network monitoring.
    *   Simulate typing the user's message into the chat input and clicking send.
    *   Monitor and stream WebSocket response messages.

## Status

This project provides a functional gateway to Copilot. Further enhancements and error handling are ongoing.

**Important Considerations:**

*   **Experimental Project**: This gateway is an experimental project. Due to the nature of interacting with Copilot via UI automation, its stability and long-term viability may be affected by changes to the Copilot website(s).
*   **Microsoft 365 Copilot Support (Highly Experimental)**:
    *   Support for Microsoft 365 Copilot (via `--copilot-type m365`) has been added and has undergone initial testing. However, it remains **highly experimental** with the following known considerations:
        *   **Behavioral Quirks**: It may sometimes attempt to execute analysis tasks (e.g., running Python code via its own capabilities) even if explicitly instructed not to within the prompt. Crafting user prompts carefully might help mitigate this.
        *   **Character Limit Impact**: The 8000-character limit can be restrictive, particularly when the gateway is used with tools like Roo Code that might send extensive context (e.g., many files in the workspace or numerous open files in the editor). This can lead to exceeding the input capacity.
    *   MS365 Copilot has different UI selectors, WebSocket behaviors (RS-separated JSON messages, full responses per update, potentially new WebSocket per prompt), and character limits (e.g., 8000 characters) compared to the standard `copilot.microsoft.com`.
    *   The implementation for MS365 Copilot in [`copilot_clients/m365_client.py`](copilot_clients/m365_client.py:1) attempts to address these differences.
    *   Configuration for MS365 Copilot (URLs, selectors) can be found and may need modification in [`config.py`](config.py:1).
*   **Character Limits**:
    *   Standard Microsoft Copilot (`copilot.microsoft.com`) typically has a character limit of around 10,240 characters.
    *   Microsoft 365 Copilot is reported to have a character limit of around 8,000 characters.
    *   The gateway now always processes the full conversation history from the client's request to construct the prompt sent to Copilot. Combined with long system prompts from the client, this can easily exceed Copilot's character limits.
    *   **No File Attachment Support**: This gateway currently does not support file attachments. Interactions are limited to text-based prompts and responses.
*   **Dedicated Debugging Profile**: For Chrome DevTools Protocol (CDP) automation, this script launches Microsoft Edge with a dedicated, separate user profile (typically located at `%TEMP%/edge_debug_profile_temp` or a similar path in your system's temporary directory, as defined by `debug_profile_dir` in [`config.py`](config.py:1)). This is a **necessary** measure due to security enhancements in Chromium-based browsers (including Edge version 136 and later).
    *   **Security Background**: Chromium intentionally restricts the use of the `--remote-debugging-port` (or `--remote-debugging-pipe`) with the *default* user data directory. This is a security measure to prevent malicious actors from exploiting the remote debugging feature to access sensitive user data, especially cookies, from the user's main profile.
    *   **Why a Separate Profile is Required**: To enable remote debugging for automation, a custom (non-default) user data directory must be specified (via the `--user-data-dir` switch). This script adheres to this requirement by creating and using a temporary profile. This ensures that the automation operates in an isolated environment, separate from your main browser profile and its data. It also uses a different encryption key for any data stored within this temporary profile, further protecting your main profile's data.
    *   **Data Handling**: Consequently, any browsing history, cookies, or site data (including logins) generated during the script's operation will be isolated to this temporary profile. If you log into any accounts or enter sensitive information, this data will reside in this separate profile directory. This script does not automatically clear this temporary profile upon exit. Please be mindful of any sensitive data that might be stored there if you perform such actions.
*   **Session Handling**:
*   The gateway attempts to detect new conversation sessions by comparing the history of `ChatMessage` objects from the client. If the current request's message history doesn't appear to be a direct continuation of the previous request's last message (checking a couple of common continuation patterns), it's considered a new session, and the Copilot page is reloaded. On the first turn of any session (including a newly reinitialized one), the gateway processes all messages from the client's request (typically including system prompts and the initial user prompt) to construct the prompt sent to Copilot. For subsequent turns within the same session, only the latest user message is processed to construct the prompt, helping to manage token limits.
*   This means that features in some AI editors that allow "re-generating" or "editing and re-sending" a previous prompt in the middle of a conversation might not work as expected, as they could be interpreted as a new session by the gateway. The gateway is designed for sequential, additive conversation flows.

### Key Enhancements

*   **Improved multi-turn conversation stability**: The handling of the WebSocket `requestId` for chat messages has been refined. The `requestId` is now captured once during the first message exchange and reused for subsequent messages in the same session. This resolves issues where responses to second and later prompts were not being correctly processed by the script.
*   **Optimized page load wait time**: Reduced the timeout for waiting for the `Page.loadEventFired` event during initial browser connection to improve startup speed.

## Requirements

*   Python 3.10+ (as defined in `pyproject.toml`)
*   `uv` (for package management, optional but recommended)
*   Required Python libraries: `websockets`, `fastapi`, `uvicorn[standard]`, `colorlog`
*   Microsoft Edge installed on Windows

To install the dependencies:

**If you use `uv` (recommended):**
```bash
uv pip install -r pyproject.toml
```
This command will install the dependencies specified in the [`pyproject.toml`](pyproject.toml:1) file.

**If you use `pip`:**
```bash
pip install websockets fastapi "uvicorn[standard]" colorlog
```
Ensure these match the dependencies listed in [`pyproject.toml`](pyproject.toml:1).

## Usage

1.  Ensure the `edge_path` variable in [`config.py`](config.py:1) points to your `msedge.exe`.

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
3.  To select the Copilot service to connect to (defaults to `standard`):
    *   `standard`: Connects to `copilot.microsoft.com`.
    *   `m365`: Connects to `m365.cloud.microsoft` (experimental, requires appropriate M365 Copilot access and may need configuration adjustments in `config.py`).
    ```bash
    python main.py --copilot-type standard
    # or
    python main.py --copilot-type m365
    ```
5.  To enable debug logging (sets log level to DEBUG and shows full prompts):
    ```bash
    python main.py --debug-logging
    ```
6.  The server will launch Edge, navigate to the selected Copilot service, and be ready to accept API requests.
7.  AI editors or clients can then be configured to use the endpoint: `http://<host>:<port>/v1/chat/completions`.

### Stdio REPL Mode

This mode allows direct command-line interaction with Copilot.

1.  Run the script with the `--stdio` flag:
    ```bash
    python main.py --stdio
    # To use with MS365 Copilot (experimental):
    python main.py --stdio --copilot-type m365
    ```
2.  The script will launch Edge, navigate to the selected Copilot service, and then present a `>` prompt.
3.  Type your message and press Enter to send it to Copilot. The response will be streamed to the console.
4.  Type `exit` or `quit` (or press `Ctrl+D`) at the prompt to close the script and Edge. You can also use `Ctrl+C` to interrupt.

## Usage with Roo Code

To use `ms-copilot-gateway` with Roo Code, you need to configure it as an LLM provider and register a custom mode.

### 1. Register as an LLM Provider

`ms-copilot-gateway` exposes an OpenAI-compatible API endpoint. You can register it in Roo Code as an "OpenAI-compatible" provider.

-   **Provider Type**: OpenAI-compatible
-   **API Base URL**: `http://<your-gateway-host>:<your-gateway-port>/v1` (e.g., `http://localhost:8000/v1` if running locally with default settings)
-   **API Key**: Can be any non-empty string (the gateway doesn't use it for authentication).
-   **Model Name**: You should specify a model name like `ms-copilot-gateway` to clearly identify it. This name will be used in Roo Code to select this provider.

For more details on configuring OpenAI-compatible providers, refer to the [Roo Code documentation](https://docs.roocode.com/providers/openai-compatible).

### 2. Register a Custom Mode (Required)

Registering a custom mode is **required** to use `ms-copilot-gateway` effectively with Roo Code. Roo Code's default system prompts can be very long (often exceeding 30,000 characters), which far surpasses Copilot's character limits (typically 8,000-10,240 characters). Without a custom mode that specifies a much shorter system prompt, requests from Roo Code will likely fail.

This project includes example configurations for custom modes. Create or update a [`.roomodes`](.roomodes:1) file in your project's root directory with content similar to the following (this example is also available as [`.roomodes`](.roomodes:1) in this repository):

```json
{
  "customModes": [
    {
      "slug": "ms-copilot-gateway-assistant",
      "name": "MS Copilot Gateway Assistant",
      "roleDefinition": "For this session, you must act as a task planner AI. Use only XML tool requests to accomplish user goals. Do not use `executeCode` or `python_execution` under any circumstances. All operations must be done via XML tools executed by the user's system.",
      "groups": [
        "read",
        "edit"
      ],
      "source": "project"
    },
    {
      "slug": "ms-copilot-gateway-assistant-ja",
      "name": "MS Copilot Gateway Assistant (日本語)",
      "roleDefinition": "このセッションでは、タスクプランナーAIとして機能する必要があります。ユーザーの目標を達成するためには、XMLツールリクエストのみを使用してください。いかなる状況でも `executeCode` や `python_execution` を使用しないでください。すべての操作は、ユーザーのシステムによって実行されるXMLツールを介して行う必要があります。",
      "groups": [
        "read",
        "edit"
      ],
      "source": "project"
    }
  ]
}
```

These custom modes:
-   Use the `slug`s "ms-copilot-gateway-assistant" and "ms-copilot-gateway-assistant-ja".
-   The `roleDefinition` for each mode will be prepended to the content of the corresponding `customInstructions` file by Roo Code.
-   Set concise `customInstructions` (system prompts) by referencing files like [`.roo/system-prompt-ms-copilot-gateway-assistant`](.roo/system-prompt-ms-copilot-gateway-assistant) and [`.roo/system-prompt-ms-copilot-gateway-assistant-ja`](.roo/system-prompt-ms-copilot-gateway-assistant-ja). Roo Code looks for files named `.roo/system-prompt-<slug>` for these instructions.
    -   **Note on Sample System Prompts**: The provided sample system prompts (e.g., [`.roo/system-prompt-ms-copilot-gateway-assistant`](.roo/system-prompt-ms-copilot-gateway-assistant)) are intentionally minimal to reduce character count for Copilot, taking into account that the `roleDefinition` is added automatically. They focus on file reading and writing operations and do **not** include instructions for more advanced Roo Code features like command execution, mode switching, or MCP tool usage. The `ms-copilot-gateway-assistant-ja` mode uses a Japanese version of the system prompt. Using a Japanese prompt with a Japanese Copilot may improve response accuracy. Additionally, Japanese can often convey more information within the same character count compared to English, which can be advantageous given Copilot's character limits, allowing for more detailed instructions.
    -   When editing or creating your own system prompts for character-limited LLMs, refer to the [Roo Code Footgun Prompting documentation](https://docs.roocode.com/features/footgun-prompting) for best practices.
-   The `groups` array defines the capabilities available in these modes (e.g., "read", "edit").

After adding or modifying the [`.roomodes`](.roomodes:1) file and ensuring the corresponding system prompt files exist with your desired short prompts, Roo Code should automatically detect the new modes. You can then select "MS Copilot Gateway Assistant" or "MS Copilot Gateway Assistant (日本語)" when interacting with this LLM.

For more information on custom modes and system prompts, see the [Roo Code documentation](https://docs.roocode.com/features/custom-modes).