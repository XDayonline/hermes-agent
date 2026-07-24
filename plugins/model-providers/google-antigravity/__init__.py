"""Google Antigravity (OAuth via agy CLI) provider profile."""

from providers import register_provider
from providers.base import ProviderProfile

google_antigravity = ProviderProfile(
    name="google-antigravity",
    aliases=(
        "agy",
        "agy-cli",
        "antigravity",
        "antigravity-oauth",
        "antigravity-cli",
        "google-antigravity-oauth",
    ),
    display_name="Google Antigravity",
    description="Google Antigravity models via OAuth (agy CLI)",
    env_vars=(),  # OAuth external — no API key
    base_url="antigravity-pa://google",
    auth_type="oauth_external",
    supports_health_check=False,
)

register_provider(google_antigravity)
