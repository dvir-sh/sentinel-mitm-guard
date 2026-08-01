import sys
import os
import time
import threading
import requests
import base64
import argparse
from urllib.parse import urlsplit

# TF/ML imports must happen before PyQt5 to avoid a known tensor-init crash
try:
    from ad_blocker import start_adblock_thread
except ImportError:
    print("could not import ad_blocker — make sure ad_blocker.py is in the same folder.")

from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)
from PyQt5.QtCore import QTimer, QObject, pyqtSignal, Qt
from PyQt5.QtGui import QFont

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

driver_ref = None


class MaliciousDialog(QDialog):
    def __init__(self, url: str, parent: QWidget = None):
        super().__init__(parent)
        self.setWindowTitle("Malicious URL Detected")
        self.setFixedSize(480, 240)
        self.setWindowFlags(
            (self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
            | Qt.WindowStaysOnTopHint
        )

        self.url    = url
        self.choice = None

        self.setStyleSheet("""
            QDialog, QWidget {
                background-color: #1a252f;
                color: #ecf0f1;
                font-family: Arial;
            }
            QLabel#bannerLabel {
                font-size: 24px;
                font-weight: bold;
                color: #f1c40f;
                padding-bottom: 10px;
            }
            QLabel#messageLabel {
                font-size: 14px;
                color: #ecf0f1;
            }
            QPushButton#closeBtn {
                background-color: #c0392b;
                color: #ffffff;
                padding: 8px 16px;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton#closeBtn:hover {
                background-color: #a93226;
            }
            QPushButton#ignoreBtn {
                background-color: #95a5a6;
                color: #2c3e50;
                padding: 8px 16px;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton#ignoreBtn:hover {
                background-color: #7f8c8d;
            }
        """)

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        banner = QLabel("MSVPN")
        banner.setObjectName("bannerLabel")
        banner.setAlignment(Qt.AlignCenter)
        layout.addWidget(banner)

        msg_label = QLabel()
        msg_label.setObjectName("messageLabel")
        msg_label.setTextFormat(Qt.RichText)
        msg_label.setTextInteractionFlags(
            Qt.TextBrowserInteraction | Qt.LinksAccessibleByMouse
        )
        msg_label.setOpenExternalLinks(True)
        msg_label.setText(
            f"A potentially malicious URL was detected:\n\n"
            f"<a href=\"{self.url}\">{self.url}</a>\n\n"
            "Do you want to close this tab or ignore the warning?"
        )
        msg_label.setWordWrap(True)
        layout.addWidget(msg_label)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        close_btn = QPushButton("Close Tab")
        close_btn.setObjectName("closeBtn")
        close_btn.clicked.connect(self._on_close)
        btn_layout.addWidget(close_btn)

        btn_layout.addSpacing(20)

        ignore_btn = QPushButton("Ignore Warning")
        ignore_btn.setObjectName("ignoreBtn")
        ignore_btn.clicked.connect(self._on_ignore)
        btn_layout.addWidget(ignore_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    def _on_close(self):
        self.choice = "close"
        self.accept()

    def _on_ignore(self):
        self.choice = "ignore"
        self.reject()

    def get_choice(self) -> str:
        return self.choice

    def exec_(self):
        self.raise_()
        self.activateWindow()
        return super().exec_()


class TabMonitor(QObject):
    malicious_detected = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.malicious_detected.connect(self.on_malicious_url)

    def on_malicious_url(self, url: str, target_id: str):
        QApplication.beep()

        dialog = MaliciousDialog(url, parent=None)
        _ = dialog.exec_()

        if dialog.get_choice() == "close":
            try:
                handles = driver_ref.window_handles

                # if this is the last tab, open a blank one before closing
                # so the browser window stays alive
                if len(handles) <= 1:
                    driver_ref.execute_cdp_cmd(
                        "Target.createTarget", {"url": "about:blank"}
                    )

                driver_ref.execute_cdp_cmd(
                    "Target.closeTarget", {"targetId": target_id}
                )
            except Exception:
                pass


def encode_url(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().strip("=")


# domains we never send to VT — they'd come back clean every time
# and burn API quota for nothing
IGNORED_DOMAINS = {
    "googleadservices.com",
    "googlesyndication.com",
    "doubleclick.net",
    "googletagmanager.com",
    "googletagservices.com",
    "google-analytics.com",
    "googleoptimize.com",
    "googletag.com",
    "g.co",
}


def get_main_host(raw_url: str) -> str:
    """Return the base domain (eTLD+1), e.g. sub.example.com -> example.com."""
    try:
        parsed = urlsplit(raw_url)
        host   = (parsed.hostname or "").lower()
        parts  = host.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host
    except Exception:
        return ""


def check_url_with_virustotal(url: str) -> bool:
    vt_api_key = "b7b3510d6136926eb092d853ea0968ca0f0df2228fdb2e302e25ea113520aca0"
    url_id  = encode_url(url)
    headers = {"x-apikey": vt_api_key}
    vt_url  = f"https://www.virustotal.com/api/v3/urls/{url_id}"

    try:
        response = requests.get(vt_url, headers=headers, timeout=10)
        if response.status_code == 200:
            stats           = response.json()["data"]["attributes"]["last_analysis_stats"]
            malicious_count = stats.get("malicious", 0)
            return malicious_count > 0
    except Exception:
        pass

    return False


def create_browser_with_tls_proxy(proxy_address: str = None):
    profile_dir = os.path.join(os.getcwd(), "selenium_profile")
    os.makedirs(profile_dir, exist_ok=True)

    chrome_options = Options()
    chrome_options.page_load_strategy = "none"

    if proxy_address:
        chrome_options.add_argument(f"--proxy-server=https://{proxy_address}")

    chrome_options.add_argument("--ignore-certificate-errors")
    chrome_options.set_capability("acceptInsecureCerts", True)
    chrome_options.add_argument(f"--user-data-dir={profile_dir}")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")
    chrome_options.add_argument("--remote-debugging-port=9222")

    driver = webdriver.Chrome(options=chrome_options)

    try:
        driver.get("https://duck.com")
    except Exception:
        pass

    return driver


def poll_tabs(seen_hosts: set, monitor: TabMonitor):
    # polls Chrome's debug endpoint every 5 seconds to check new tab URLs.
    # each unique base domain is checked once per session to avoid repeat VT lookups.
    debug_url = "http://127.0.0.1:9222/json"

    while True:
        try:
            r    = requests.get(debug_url, timeout=3)
            tabs = r.json()

            for tab in tabs:
                url       = tab.get("url", "")
                target_id = tab.get("id", "")

                if url.startswith("http://") or url.startswith("https://"):
                    host = get_main_host(url)

                    if not host:
                        continue
                    if host in IGNORED_DOMAINS:
                        continue
                    if host not in seen_hosts:
                        seen_hosts.add(host)
                        if check_url_with_virustotal(url):
                            monitor.malicious_detected.emit(url, target_id)

            time.sleep(5)

        except requests.exceptions.RequestException:
            time.sleep(2)
        except Exception:
            time.sleep(2)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    parser = argparse.ArgumentParser()
    parser.add_argument("--ad-block", action="store_true", help="Enable ML Ad Blocker")
    parser.add_argument("--no-proxy", action="store_true", help="Launch without proxy")
    args, unknown = parser.parse_known_args()

    proxy  = None if args.no_proxy else "shortline.proxy.rlwy.net:22343"
    driver = create_browser_with_tls_proxy(proxy)
    driver_ref = driver

    if args.ad_block:
        if 'start_adblock_thread' in globals() or 'start_adblock_thread' in locals():
            start_adblock_thread(driver)

    monitor    = TabMonitor()
    seen_hosts = set()

    poll_thread = threading.Thread(
        target=poll_tabs, args=(seen_hosts, monitor), daemon=True
    )
    poll_thread.start()

    # keep-alive timer so the Qt event loop doesn't lose track of the driver
    def keep_driver_alive():
        try:
            _ = driver.window_handles
        except Exception:
            pass

    timer = QTimer()
    timer.timeout.connect(keep_driver_alive)
    timer.start(1000)

    sys.exit(app.exec_())
