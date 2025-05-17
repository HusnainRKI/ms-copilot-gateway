import logging
from typing import Optional

from config import AppSettings, CopilotSettings as IndividualCopilotSettings # Renamed to avoid clash
from .base_client import BaseCopilotClient
from .standard_client import StandardCopilotClient
from .m365_client import M365CopilotClient

logger = logging.getLogger("CopilotClient.Factory")

class CopilotClientFactory:
    @staticmethod
    def create_client(app_settings: AppSettings) -> Optional[BaseCopilotClient]:
        """
        Creates a Copilot client instance based on the copilot_type specified in app_settings.
        """
        copilot_type = app_settings.copilot_type
        active_copilot_config: IndividualCopilotSettings = app_settings.get_active_copilot_settings()

        logger.info(f"Attempting to create Copilot client of type: {copilot_type}")

        if copilot_type == "standard":
            return StandardCopilotClient(
                edge_path=app_settings.edge_path,
                debug_profile_dir=app_settings.debug_profile_dir,
                debugging_port=app_settings.debugging_port,
                copilot_url=active_copilot_config.copilot_url,
                websocket_url_filter=active_copilot_config.websocket_url_filter,
                user_input_selector=active_copilot_config.user_input_selector,
                submit_button_selector=active_copilot_config.submit_button_selector,
                is_debug_logging=app_settings.debug_logging
            )
        elif copilot_type == "m365":
            return M365CopilotClient(
                edge_path=app_settings.edge_path,
                debug_profile_dir=app_settings.debug_profile_dir,
                debugging_port=app_settings.debugging_port,
                copilot_url=active_copilot_config.copilot_url,
                websocket_url_filter=active_copilot_config.websocket_url_filter,
                user_input_selector=active_copilot_config.user_input_selector,
                submit_button_selector=active_copilot_config.submit_button_selector,
                is_debug_logging=app_settings.debug_logging
            )
        else:
            logger.error(f"Unknown Copilot type specified: {copilot_type}")
            return None