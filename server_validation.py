"""Telegram Test Server IP Validation

This module provides kill-switch functionality to prevent accidental
connections to production Telegram servers during E2E testing.
"""


def validate_test_server_ip(ip: str, context: str = "Connection") -> None:
    """KILL-SWITCH: Validate that an IP is a known Telegram test server.

    Args:
        ip: IP address to validate
        context: Description of where this check is performed (for error messages)

    Raises:
        RuntimeError: If IP appears to be a production server
    """
    SAFE_TEST_IPS = [
        "149.154.167.",  # DC2 test server range
        "149.154.175.",  # DC1/DC3 test server ranges
        "127.0.0.1",     # Localhost for development
    ]

    is_safe = any(ip.startswith(prefix) for prefix in SAFE_TEST_IPS)

    if not is_safe:
        raise RuntimeError(
            f"🚨 PRODUCTION KILL-SWITCH ACTIVATED 🚨\n\n"
            f"DANGER: {context} attempted to connect to {ip}\n"
            f"This does NOT match any known Telegram test server IP patterns.\n"
            f"Safe IPs must start with: {', '.join(SAFE_TEST_IPS)}\n\n"
            f"To prevent accidental damage to production data, operation aborted.\n"
            f"If you need to use a different test server, update SAFE_TEST_IPS\n"
            f"in server_validation.py and ensure it's a real test server.\n"
        )
