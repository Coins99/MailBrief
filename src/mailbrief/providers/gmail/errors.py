"""Safe, application-authored Gmail setup errors suitable for diagnostic output."""

from mailbrief.errors import ConfigurationError


class GmailSetupError(ConfigurationError):
    """Use only static guidance: never include paths, payloads or backend exceptions."""
