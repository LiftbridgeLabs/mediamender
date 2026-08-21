"""Canonical product branding constants."""

import os

PRODUCT_NAME = "mediaMender"
PRODUCT_SLUG = "mediamender"
ENV_PREFIX = "MEDIAMENDER_"


def get_env(name: str, default: str = "") -> str:
    """Read a mediaMender deployment environment variable."""
    return os.environ.get(name, default)
