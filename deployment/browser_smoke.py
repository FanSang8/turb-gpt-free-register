"""Exercise real Firefox and authenticated proxying without external accounts."""
import base64
import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from config import camoufox as cfg
from config import proxy as proxy_cfg
from core.camoufox_driver import build_camoufox_driver

HTML = b"""<!doctype html><title>Camoufox smoke</title>
<input id="email"><button id="submit" onclick="document.title='clicked'">Go</button>"""
seen = []


class ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.headers.get("Proxy-Authorization") == (
            "Basic " + base64.b64encode(b"smoke:password").decode()
        )
        seen.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(HTML)))
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *_):
        pass


def exercise(headless):
    cfg.CAMOUFOX_HEADLESS = headless
    cfg.CAMOUFOX_HUMANIZE = False
    cfg.CAMOUFOX_GEOIP = False
    cfg.CAMOUFOX_USE_PROXY = True
    cfg.CAMOUFOX_TIMEOUT = 20
    proxy_cfg.PROXY_POOL_UPSTREAM_PROXY = ""
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    driver = None
    try:
        driver, opened = build_camoufox_driver(
            f"http://smoke:password@127.0.0.1:{server.server_port}",
        )
        relay = driver._proxy_relay
        relay_port = urlsplit(relay.proxy_url).port
        driver.get("http://camoufox.test/smoke")
        element = driver.find_element("css selector", "#email")
        element.send_keys("smoke@example.test")
        assert driver.execute_script("return arguments[0].value;", element) == "smoke@example.test"
        assert driver.execute_async_script(
            "const done = arguments[arguments.length - 1]; setTimeout(() => done(arguments[0] + 1), 5);", 41,
        ) == 42
        driver.find_element("css selector", "#submit").click()
        assert driver.execute_script("return document.title;") == "clicked"
        assert not hasattr(driver, "execute_cdp_cmd")
        assert "Firefox/" in driver.execute_script("return navigator.userAgent;")
        traffic = driver.proxy_transport_snapshot()
        assert traffic["total_bytes"] > 0
        assert opened.raw["driver"] == "camoufox"
        kept = driver
        driver, _ = build_camoufox_driver(
            f"http://smoke:password@127.0.0.1:{server.server_port}",
        )
        assert kept._manager is None
        driver.get("http://camoufox.test/replacement")
        assert driver.execute_script("return document.title;") == "Camoufox smoke"
        driver.quit()
        driver = None
        try:
            with socket.create_connection(("127.0.0.1", relay_port), timeout=0.5):
                raise AssertionError("proxy relay still listening after quit")
        except OSError:
            pass
    finally:
        if driver:
            driver.quit()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert seen
    print(f"CAMOUFOX_SMOKE_OK headless={headless}", flush=True)


if __name__ == "__main__":
    exercise(True)
    exercise(True)  # A second launch detects a leaked Playwright sync event loop.
    if os.name != "nt":
        display = subprocess.Popen(
            ["Xvfb", ":98", "-screen", "0", "1600x1000x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            os.environ["DISPLAY"] = ":98"
            subprocess.run(
                ["sh", "-c", "for i in $(seq 1 40); do xdpyinfo -display :98 >/dev/null 2>&1 && exit 0; sleep 0.25; done; exit 1"],
                check=True, timeout=15,
            )
            exercise(False)
        finally:
            display.terminate()
            display.wait(timeout=10)
