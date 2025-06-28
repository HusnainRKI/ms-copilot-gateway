import os
import tempfile
from pydantic import BaseModel, Field
from typing import Dict, Any

class CopilotSettings(BaseModel):
    """Individual Copilot type settings."""
    copilot_url: str
    websocket_url_filter: str
    user_input_selector: str
    submit_button_selector: str
    # Placeholder for character limits, might be useful later
    # character_limit: int = 10240 

class AppSettings(BaseModel):
    """Application-wide settings."""
    # General settings
    debug_logging: bool = False
    host: str = "0.0.0.0"
    port: int = 8000

    # Edge and CDP settings
    edge_path: str = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
    debug_profile_dir: str = Field(default_factory=lambda: os.path.join(tempfile.gettempdir(), "edge_debug_profile_temp"))
    debugging_port: int = 9222

    # Standard Copilot specific settings
    standard_copilot: CopilotSettings = CopilotSettings(
        copilot_url="https://copilot.microsoft.com/",
        websocket_url_filter="wss://copilot.microsoft.com/c/api/chat?api-version=2",
        user_input_selector="textarea#userInput",
        submit_button_selector='button[data-testid="submit-button"]'
    )

    # MS365 Copilot specific settings (placeholders, to be confirmed/updated)
    m365_copilot: CopilotSettings = CopilotSettings(
        copilot_url="https://m365.cloud.microsoft/chat", # Example, might need query params
        websocket_url_filter="wss://substrate.office.com/m365Copilot/Chathub/", # Example, likely needs more specifics
        user_input_selector="span[role=textbox]", # Updated selector
        submit_button_selector='button[type=submit]' # Updated selector
        # character_limit=8000 # For M365
    )

    # To select which copilot config to use
    copilot_type: str = "standard" # Default to standard

    def get_active_copilot_settings(self) -> CopilotSettings:
        if self.copilot_type == "m365":
            return self.m365_copilot
        return self.standard_copilot

# Global instance of settings
settings = AppSettings()