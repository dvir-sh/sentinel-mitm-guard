import os
import sys
import requests
import subprocess
import tempfile
import time
import certifi
import socket
import platform
import argparse

# TF/ML imports must happen before PyQt5 to avoid a known tensor-init crash
try:
    from ad_blocker import start_adblock_thread
except ImportError:
    pass

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QWidget,
    QLabel,
    QVBoxLayout,
)
from PyQt5.QtGui import QFont, QColor

PROXY_HOST = os.getenv("PROXY_HOST", "yamanote.proxy.rlwy.net:27125")
CA_PATH            = None
REQUESTS_CA_BUNDLE = None

_MAX_RETRIES = 3
_RETRY_DELAY = 3  # seconds between retry attempts


def wake_proxy(host_with_port: str, wait_secs: int = 5):
    # sends a lightweight connection attempt to wake the VPS from sleep,
    # falls back to ping if the TCP connect fails
    host, _, port_str = host_with_port.rpartition(":")
    try:
        port = int(port_str)
    except ValueError:
        host = host_with_port
        port = 8443

    try:
        with socket.create_connection((host, port), timeout=2):
            pass
    except Exception:
        param = "-n" if platform.system().lower() == "windows" else "-c"
        try:
            subprocess.run(
                ["ping", param, "1", host],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

    time.sleep(wait_secs)


def fetch_and_install_ca():
    global CA_PATH, REQUESTS_CA_BUNDLE

    # wake the VPS once before downloading — intentionally outside the retry loop
    # to avoid hammering it with unnecessary pings
    wake_proxy(PROXY_HOST, wait_secs=3)

    ca_url = f"https://{PROXY_HOST}/mitmproxy-ca-cert.pem"

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.get(ca_url, verify=False, timeout=10, stream=True)
            resp.raise_for_status()

            fd, CA_PATH = tempfile.mkstemp(suffix=".pem")
            with os.fdopen(fd, "wb") as f:
                for chunk in resp.iter_content(1024):
                    f.write(chunk)
            last_error = None
            break
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY)

    if last_error is not None:
        raise RuntimeError(
            f"Failed to download CA from {ca_url} "
            f"after {_MAX_RETRIES} attempts: {last_error}"
        )

    # merge the mitmproxy cert with the system bundle so requests still trusts
    # regular sites while also accepting the proxy's self-signed cert
    default_bundle = certifi.where()
    merged_fd, merged_path = tempfile.mkstemp(suffix=".pem")
    with os.fdopen(merged_fd, "wb") as out:
        with open(default_bundle, "rb") as sys_certs:
            out.write(sys_certs.read())
        with open(CA_PATH, "rb") as mitm_cert:
            out.write(mitm_cert.read())
    REQUESTS_CA_BUNDLE = merged_path
    os.environ['REQUESTS_CA_BUNDLE'] = merged_path

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            subprocess.run(
                ["certutil", "-user", "-addstore", "Root", CA_PATH],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            last_error = None
            break
        except subprocess.CalledProcessError as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY)

    if last_error is not None:
        raise RuntimeError(
            f"Failed to add CA to Windows store "
            f"after {_MAX_RETRIES} attempts: {last_error}"
        )


def launch_selenium(target_url: str):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    chrome_opts = Options()
    chrome_opts.page_load_strategy = "none"
    chrome_opts.add_argument(f"--proxy-server=https://{PROXY_HOST}")
    chrome_opts.add_argument("--user-data-dir=/tmp/mitmtest")
    chrome_opts.add_argument("--disable-extensions")
    chrome_opts.add_argument("--disable-http2")

    driver = webdriver.Chrome(options=chrome_opts)
    driver.get(target_url)
    return driver


class CAInstallWorker(QThread):
    finished_ok    = pyqtSignal()
    error_occured  = pyqtSignal(str)

    def run(self):
        try:
            fetch_and_install_ca()
            self.finished_ok.emit()
        except Exception as e:
            self.error_occured.emit(str(e))


class LoadingWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowTitleHint |
            Qt.CustomizeWindowHint |
            Qt.WindowStaysOnTopHint
        )
        self.setFixedSize(300, 100)
        self.setStyleSheet("""
            QWidget {
                background-color: #1a252f;
                color: #ecf0f1;
            }
            QLabel {
                font-size: 14px;
                color: #ecf0f1;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setAlignment(Qt.AlignCenter)

        self.label = QLabel("Downloading certificate...")
        font = QFont("Arial", 12, QFont.Bold)
        self.label.setFont(font)
        self.label.setAlignment(Qt.AlignCenter)

        layout.addWidget(self.label)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ad-block", action="store_true", help="Enable ML Ad Blocker")
    args, unknown = parser.parse_known_args()

    app = QApplication(sys.argv)

    loader = LoadingWindow()
    loader.setWindowTitle("Please wait")
    loader.show()

    worker = CAInstallWorker()

    def on_success():
        loader.close()

        url = unknown[0] if len(unknown) > 0 else "https://duck.com"

        try:
            driver = launch_selenium(url)

            if args.ad_block:
                if 'start_adblock_thread' in globals():
                    start_adblock_thread(driver)

        except Exception as e:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.critical(
                None,
                "Error launching browser",
                f"Failed to launch Selenium browser:\n{e}"
            )
            sys.exit(1)

        input("Press Enter to quit...")
        driver.quit()

        for path in (CA_PATH, REQUESTS_CA_BUNDLE):
            if path and os.path.exists(path):
                time.sleep(1)
                os.remove(path)

        sys.exit(0)

    def on_error(message: str):
        from PyQt5.QtWidgets import QMessageBox
        loader.close()
        QMessageBox.critical(
            None,
            "Certificate Error",
            f"{message}\n\nAborting."
        )
        sys.exit(1)

    worker.finished_ok.connect(on_success)
    worker.error_occured.connect(on_error)
    worker.start()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
