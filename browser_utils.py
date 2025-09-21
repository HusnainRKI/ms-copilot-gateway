"""
Browser detection and path utilities for cross-platform support.
"""
import os
import platform
import shutil
from typing import Optional, List


def get_platform_default_browser_paths() -> List[str]:
    """
    Get a list of common browser paths for the current platform.
    Returns paths in order of preference.
    """
    system = platform.system().lower()
    
    if system == "linux":
        return [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable", 
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable"
        ]
    elif system == "darwin":  # macOS
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        ]
    elif system == "windows":
        return [
            "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
            "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
        ]
    else:
        return []


def find_available_browser() -> Optional[str]:
    """
    Find the first available browser on the system.
    Returns the path to the browser executable or None if none found.
    """
    # First try system PATH
    for browser_name in ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "msedge", "chrome"]:
        browser_path = shutil.which(browser_name)
        if browser_path:
            return browser_path
    
    # Then try platform-specific paths
    for browser_path in get_platform_default_browser_paths():
        if os.path.isfile(browser_path) and os.access(browser_path, os.X_OK):
            return browser_path
    
    return None


def get_browser_name(browser_path: str) -> str:
    """
    Get a human-readable name for the browser based on its path.
    """
    browser_path_lower = browser_path.lower()
    
    if "chrome" in browser_path_lower:
        return "Chrome"
    elif "chromium" in browser_path_lower:
        return "Chromium"
    elif "edge" in browser_path_lower or "msedge" in browser_path_lower:
        return "Edge"
    else:
        return "Browser"


def get_cross_platform_browser_args(browser_path: str, debugging_port: int, debug_profile_dir: Optional[str] = None) -> List[str]:
    """
    Get browser arguments that work across different Chromium-based browsers.
    """
    args = [
        browser_path,
        f"--remote-debugging-port={debugging_port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-restore-session-state",
        "--restore-last-session=false",
        "--disable-session-crashed-bubble",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-extensions",
        "--disable-plugins",
        "--disable-default-apps"
    ]
    
    # Add headless mode and Docker-specific args for Linux containers
    if platform.system().lower() == "linux":
        args.extend([
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-software-rasterizer",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=TranslateUI",
            "--disable-ipc-flooding-protection",
            "--disable-crash-reporter",
            "--disable-component-extensions-with-background-pages",
            "--single-process"  # Run in single process mode for containers
        ])
    
    if debug_profile_dir:
        args.append(f"--user-data-dir={debug_profile_dir}")
    
    return args