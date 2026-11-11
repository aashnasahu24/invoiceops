"""Provider errors shared by adapters and the resilient extraction wrapper."""


class ProviderError(Exception):
    """Base class for failures reported by an LLM provider."""


class ProviderRateLimitError(ProviderError):
    """The provider rejected the request due to rate or quota limits."""


class ProviderTimeoutError(ProviderError):
    """The provider request timed out."""


class ProviderUnavailableError(ProviderError):
    """The provider is temporarily unavailable."""


class ProviderResponseError(ProviderError):
    """The provider returned a blocked or unusable response."""
