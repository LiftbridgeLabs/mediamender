"""Product branding and migration compatibility constants."""

import os

PRODUCT_NAME = "mediaWarden"
PRODUCT_SLUG = "mediawarden"
LEGACY_NAME = "emptyarr"
LEGACY_SLUG = "emptyarr"

# Existing environment variables, Docker names, and data directories remain
# supported during the rebrand. New deployment examples use MEDIAWARDEN_* where
# an environment override is exposed.
ENV_PREFIX = "MEDIAWARDEN_"
LEGACY_ENV_PREFIX = "EMPTYARR_"


def env_aliases(name: str) -> tuple[str, str]:
    """Return the new and legacy environment names for a branded variable."""
    if name.startswith(LEGACY_ENV_PREFIX):
        return ENV_PREFIX + name[len(LEGACY_ENV_PREFIX):], name
    if name.startswith(ENV_PREFIX):
        return name, LEGACY_ENV_PREFIX + name[len(ENV_PREFIX):]
    return name, name


def get_env(name: str, default: str = "") -> str:
    """Read a new MEDIAWARDEN variable, then its EMPTYARR compatibility alias."""
    primary, legacy = env_aliases(name)
    return os.environ.get(primary) or os.environ.get(legacy, default)
