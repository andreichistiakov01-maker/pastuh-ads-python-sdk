"""Pastuh Ads Partner API — асинхронный Python-клиент.

Быстрый старт::

    async with PastuhAdsClient(
        base_url="https://pastuhads.com/api/partner/v1",
        api_key="opk_...",
        source_id=50,
        placement_id=1,
    ) as client:
        me = await client.get_me()
"""

from .client import PastuhAdsClient
from .gender import gender_from_user
from .interactive import (
    CB_CHECK,
    CB_OPEN,
    CB_REOPEN,
    InteractiveOpBlock,
)
from .observation import user_observation_from_user
from .exceptions import (
    ApiConnectionError,
    ApiTimeoutError,
    AuthenticationError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PastuhAdsError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from .models import (
    BotStartConfirmResponse,
    CallbackClickResponse,
    ClickResponse,
    MeResponse,
    OpListResponse,
    OpTask,
    PartnerInfo,
    Payout,
    PayoutsSummary,
    Placement,
    PremiumClaimStatus,
    PremiumVerificationInfo,
    PremiumVerificationResponse,
    SessionStatusResponse,
    SessionSyncItem,
    SessionSyncResponse,
    ShownResponse,
    SourceInfo,
)

from ._version import __version__ as __version__

__all__ = [
    "PastuhAdsClient",
    "InteractiveOpBlock",
    "CB_OPEN",
    "CB_REOPEN",
    "CB_CHECK",
    "gender_from_user",
    "user_observation_from_user",
    "PastuhAdsError",
    "AuthenticationError",
    "ForbiddenError",
    "NotFoundError",
    "ConflictError",
    "ValidationError",
    "RateLimitError",
    "ServerError",
    "ApiTimeoutError",
    "ApiConnectionError",
    "MeResponse",
    "PartnerInfo",
    "SourceInfo",
    "PayoutsSummary",
    "OpListResponse",
    "OpTask",
    "PremiumClaimStatus",
    "PremiumVerificationInfo",
    "PremiumVerificationResponse",
    "ShownResponse",
    "SessionStatusResponse",
    "SessionSyncItem",
    "SessionSyncResponse",
    "ClickResponse",
    "CallbackClickResponse",
    "BotStartConfirmResponse",
    "Payout",
    "Placement",
]
