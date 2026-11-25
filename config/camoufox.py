"""Local Camoufox Firefox settings."""
from config.env_loader import apply_env_overrides

CAMOUFOX_HEADLESS: bool = True
CAMOUFOX_HUMANIZE: bool = True
CAMOUFOX_GEOIP: bool = True
CAMOUFOX_USE_PROXY: bool = True
CAMOUFOX_LOCALE: str = ""
CAMOUFOX_TIMEOUT: int = 90
CAMOUFOX_KEEP_BROWSER_OPEN: bool = False

apply_env_overrides(globals(), {
    "CAMOUFOX_HEADLESS": "bool",
    "CAMOUFOX_HUMANIZE": "bool",
    "CAMOUFOX_GEOIP": "bool",
    "CAMOUFOX_USE_PROXY": "bool",
    "CAMOUFOX_LOCALE": "str",
    "CAMOUFOX_TIMEOUT": "int",
    "CAMOUFOX_KEEP_BROWSER_OPEN": "bool",
})
