"""Successful registration delivery settings."""
from config.env_loader import apply_env_overrides, env_value

REGISTER_PUSH_ONLY = True
PUSH_API_URL = ""
PUSH_API_TOKEN = ""
PUSH_API_TIMEOUT = 15
PUSH_API_VERIFY_TLS = True
PUSH_API_INCLUDE_PROXY = True
PUSH_INSTANCE_LABEL = ""
FEISHU_WEBHOOK_URL = ""

apply_env_overrides(globals())
for _key in ("PUSH_API_URL", "PUSH_API_TOKEN", "PUSH_API_TIMEOUT", "PUSH_API_VERIFY_TLS",
             "PUSH_API_INCLUDE_PROXY"):
    globals()[_key] = env_value("REGISTER_" + _key, globals()[_key])
