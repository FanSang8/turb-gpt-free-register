"""Camoufox entry point, sharing the maintained browser page workflow."""
from config import camoufox as _cfg
from core.camoufox_driver import build_camoufox_driver
from core.cloakbrowser_registration import run_browser_registration


def run_camoufox_registration(
    email, name, birthday, proxy=None, otp_code=None, batch_dir=None,
    on_email_acquired=None,
):
    return run_browser_registration(
        email=email, name=name, birthday=birthday, proxy=proxy,
        otp_code=otp_code, batch_dir=batch_dir,
        on_email_acquired=on_email_acquired,
        driver_builder=build_camoufox_driver,
        keep_browser_open=bool(_cfg.CAMOUFOX_KEEP_BROWSER_OPEN),
        label="Camoufox", metadata_key="camoufox", supports_codex=False,
    )
