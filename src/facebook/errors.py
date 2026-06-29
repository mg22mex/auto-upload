class FacebookAutomationError(Exception):
    """Base error for Facebook Playwright automation."""


class FacebookSessionError(FacebookAutomationError):
    """Login/session is missing or expired."""


class FacebookPostingError(FacebookAutomationError):
    """Failed to create, update, or remove a listing."""
