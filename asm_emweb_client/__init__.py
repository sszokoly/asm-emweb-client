import logging

__version__ = "0.2.0"

from .client import AsmEmWebClient, AsyncAsmEmWebClient
from .config import Config, ConfigError, resolve_config
from .diagnostics import logger
from .models import ApiError, NotifyResult, NotifyStatus, Registration, RegistrationsPage

logger.addHandler(logging.NullHandler())

__all__ = [
    "__version__",
    "AsmEmWebClient",
    "AsyncAsmEmWebClient",
    "ApiError",
    "NotifyResult",
    "NotifyStatus",
    "Registration",
    "RegistrationsPage",
    "Config",
    "ConfigError",
    "resolve_config",
]
