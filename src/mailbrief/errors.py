"""Application-level errors that are not tied to an external provider."""


class ConfigurationError(RuntimeError):
    """Required nonsecret application configuration is missing or unsafe."""
