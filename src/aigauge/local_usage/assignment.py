"""Which account a provider's local usage counts against."""

from __future__ import annotations

from ..config import Config, LocalUsageProviderConfig, browser_account
from .store import PROVIDERS


def provider_config(config: Config, provider: str) -> LocalUsageProviderConfig:
    return getattr(config.local_usage, provider)


def assigned_account(config: Config, provider: str) -> str | None:
    """The account a provider's local usage counts against, if tracking is active."""
    if not config.local_usage.enabled:
        return None
    settings = provider_config(config, provider)
    if not settings.enabled or not settings.account_id:
        return None
    account = browser_account(config, settings.account_id)
    if account is None or account.kind != provider:
        return None  # removed or changed account: tracking pauses
    return account.id


def provider_for_account(config: Config, account_id: str) -> str | None:
    for provider in PROVIDERS:
        if assigned_account(config, provider) == account_id:
            return provider
    return None


def comparison_period_id(config: Config, provider: str) -> str:
    """Windows are only compared with windows from the same period.

    Changing the assigned account or the log folder starts a new one.
    """
    settings = provider_config(config, provider)
    return f"{provider}|{settings.account_id or ''}|{settings.log_root or ''}"
