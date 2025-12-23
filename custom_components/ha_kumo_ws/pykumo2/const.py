"""Constants for the Mitsubishi Comfort (Kumo Cloud) client."""

BASE_URL = "https://app-prod.kumocloud.com"
SOCKET_URL = "https://socket-prod.kumocloud.com"

# Captured from mobile application traffic
APP_VERSION = "3.2.4"
USER_AGENT = "kumocloud/1237 CFNetwork/3860.200.71 Darwin/25.1.0"

DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "x-app-version": APP_VERSION,
    "app-env": "prd",
    "Content-Type": "application/json",
    "User-Agent": USER_AGENT,
    "Cache-Control": "no-cache, no-store",
    "x-allow-cache": "false",
}

# Request types used to force adapters to respond with fresh data
DEFAULT_FORCE_REQUESTS: tuple[str, ...] = ("iuStatus", "profile", "adapterStatus", "mhk2")
