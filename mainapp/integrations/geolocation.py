import json
import urllib.error
import urllib.parse
import urllib.request
from django.conf import settings


LOCATION_REFUSAL_MESSAGE = (
    "This connection-location check could not confirm a connection in Ghana, "
    "so registration stops here."
)

ANONYMITY_FLAGS = ("vpn", "proxy", "tor", "relay", "hosting")


def resolve_client_ip(request) -> str:
    """
    Resolve the connection IP address for the signup location check.
    - When DEBUG is True and GEO_LOOKUP_IP is set in server settings, use GEO_LOOKUP_IP.
    - Otherwise use REMOTE_ADDR, consulting X-Forwarded-For only when REMOTE_ADDR
      is listed in TRUSTED_PROXY_IPS.
    """
    if getattr(settings, "DEBUG", False):
        debug_ip = (getattr(settings, "GEO_LOOKUP_IP", "") or "").strip()
        if debug_ip:
            return debug_ip

    remote_addr = (request.META.get("REMOTE_ADDR") or "").strip()
    trusted_proxies = set(getattr(settings, "TRUSTED_PROXY_IPS", None) or [])

    if remote_addr and remote_addr in trusted_proxies:
        xff = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
        if xff:
            parts = [part.strip() for part in xff.split(",") if part.strip()]
            for candidate in reversed(parts):
                if candidate not in trusted_proxies:
                    return candidate
            if parts:
                return parts[0]

    return remote_addr


def fetch_ipinfo(ip_address: str) -> dict:
    """
    Call IPinfo for the given IP address and return the parsed JSON object.
    Raises ValueError or OSError on network/timeout/parse failure.
    Never logs the token or response body.
    """
    token = (getattr(settings, "IPINFO_TOKEN", "") or "").strip()
    if not token:
        raise ValueError("Missing geolocation provider token.")

    quoted_ip = urllib.parse.quote(ip_address, safe=":")
    url = f"https://ipinfo.io/{quoted_ip}/json"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )
    timeout = float(getattr(settings, "GEO_LOOKUP_TIMEOUT", 3.0) or 3.0)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("Non-200 status from geolocation provider.")
        raw_body = response.read().decode("utf-8")

    parsed = json.loads(raw_body)
    if not isinstance(parsed, dict):
        raise ValueError("Provider response is not a JSON object.")
    return parsed


def is_clearly_ghana_payload(payload: object) -> bool:
    """
    Map only the documented IPinfo fields:
    - country == "GH"
    - privacy.vpn, privacy.proxy, privacy.tor, privacy.relay, privacy.hosting
      must all be present and strictly False.
    """
    if not isinstance(payload, dict):
        return False

    country = payload.get("country")
    if not isinstance(country, str) or country != "GH":
        return False

    privacy = payload.get("privacy")
    if not isinstance(privacy, dict):
        return False

    for flag in ANONYMITY_FLAGS:
        if flag not in privacy:
            return False
        value = privacy[flag]
        if not isinstance(value, bool) or value is not False:
            return False

    return True


def check_request_connection_is_clearly_ghana(request) -> bool:
    """
    Resolve the connection IP from the request and verify via IPinfo that
    the connection is clearly in Ghana with no anonymity or hosting flags.
    """
    if getattr(settings, "BYPASS_GEO_CHECK", False) is True:
        return True

    ip_address = resolve_client_ip(request)
    if not ip_address:
        return False

    try:
        payload = fetch_ipinfo(ip_address)
    except Exception:
        return False

    return is_clearly_ghana_payload(payload)
