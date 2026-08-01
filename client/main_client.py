import os
import sys
import re
import json
import base64
import hashlib
import shutil
import requests
import subprocess
from PyQt5.QtWidgets import (
    QApplication,
    QWidget,
    QPushButton,
    QLabel,
    QHBoxLayout,
    QVBoxLayout,
    QLineEdit,
    QComboBox,
    QFrame,
    QToolButton,
    QSizePolicy,
    QSpacerItem,
    QGraphicsDropShadowEffect,
    QMessageBox,
    QCheckBox,
    QDialog,
    QProgressBar,
    QTextEdit,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QIcon, QPixmap, QColor

SERVER_URL = "http://tramway.proxy.rlwy.net:36453"
SHARED_KEY = "3KfJ7$6AM9Qp#XaV0zzb3rY"

_MAX_RETRIES = 3
_RETRY_DELAY = 3  # seconds between attempts

# Supabase storage for debug session uploads (free tier, private bucket)
SUPABASE_URL      = "https://ldcrqejpnnlocfdycqfc.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxkY3JxZWpwbm5sb2NmZHljcWZjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzgyODc0MzMsImV4cCI6MjA5Mzg2MzQzM30.L2gCH292MtEY9q_Hxu5D0WoO_pOT1FxXsvpGRaeUAbo"
SUPABASE_BUCKET   = "debug-passes"

# must match DEBUG_OUTPUT_DIR in ad_blocker.py
DEBUG_OUTPUT_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_passes")


def _post_with_retry(url: str, data: dict, timeout: int = 5):
    """
    POST to url with up to _MAX_RETRIES attempts.
    Only retries on network-level errors — bad credentials or server errors come back immediately.
    Returns the Response on success, or raises the last RequestException.
    """
    import time
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return requests.post(url, data=data, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY)
    raise last_exc


# simple XOR stream cipher, same logic as the server side
def _xor_bytes(data: bytes, key: bytes) -> bytes:
    out     = bytearray(len(data))
    key_len = len(key)
    for i, b in enumerate(data):
        out[i] = b ^ key[i % key_len]
    return bytes(out)

def encrypt_and_encode(plaintext: str) -> str:
    raw       = plaintext.encode("utf-8")
    key_bytes = SHARED_KEY.encode("utf-8")
    xored     = _xor_bytes(raw, key_bytes)
    b64       = base64.b64encode(xored)
    return b64.decode("ascii")

def decode_and_decrypt(cipher_b64: str) -> str:
    try:
        xored     = base64.b64decode(cipher_b64)
        key_bytes = SHARED_KEY.encode("utf-8")
        raw       = _xor_bytes(xored, key_bytes)
        return raw.decode("utf-8")
    except Exception:
        return ""


def _hash_file(path: str) -> str:
    """SHA-256 of a file's raw bytes — used for fast duplicate detection."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def deduplicate_session(session_dir: str) -> tuple:
    """
    Walk passes in a session in order and delete any pass whose _original.png
    is identical to the immediately preceding kept pass. This removes passes
    where nothing changed on screen between captures.

    All three files for a duplicate pass are removed:
      pass_NNNN_original.png  pass_NNNN_debug.png  pass_NNNN_meta.json

    Returns (kept, removed).
    """
    pattern = re.compile(r"^pass_(\d+)_meta\.json$")
    passes  = sorted(name for name in os.listdir(session_dir) if pattern.match(name))

    prev_hash = None
    kept = removed = 0

    for meta_name in passes:
        m      = pattern.match(meta_name)
        prefix = f"pass_{m.group(1).zfill(4)}"
        orig   = os.path.join(session_dir, f"{prefix}_original.png")

        if not os.path.exists(orig):
            kept += 1
            continue

        current_hash = _hash_file(orig)

        if current_hash == prev_hash:
            for suffix in ("_original.png", "_debug.png", "_meta.json"):
                fp = os.path.join(session_dir, prefix + suffix)
                if os.path.exists(fp):
                    os.remove(fp)
            removed += 1
        else:
            prev_hash = current_hash
            kept += 1

    return kept, removed


class DebugUploaderThread(QThread):
    """
    Background thread that deduplicates every session, uploads surviving files
    to Supabase Storage, then deletes the local session folder on success.
    Config is passed in the constructor so the thread doesn't rely on globals.
    """
    progress = pyqtSignal(str)        # log line for the UI
    finished = pyqtSignal(bool, str)  # (success, summary)

    def __init__(self, supabase_url: str, anon_key: str, bucket: str, debug_dir: str):
        super().__init__()
        self._url       = supabase_url
        self._key       = anon_key
        self._bucket    = bucket
        self._debug_dir = debug_dir

    def run(self):
        if not os.path.isdir(self._debug_dir):
            self.finished.emit(False, "No debug_passes folder found - nothing to upload.")
            return

        sessions = sorted(
            d for d in os.listdir(self._debug_dir)
            if os.path.isdir(os.path.join(self._debug_dir, d))
        )
        if not sessions:
            self.finished.emit(False, "No session folders found - nothing to upload.")
            return

        total_kept = total_removed = total_uploaded = sessions_ok = 0

        for session in sessions:
            session_dir = os.path.join(self._debug_dir, session)

            self.progress.emit(f"Deduplicating {session}...")
            kept, removed  = deduplicate_session(session_dir)
            total_kept    += kept
            total_removed += removed
            self.progress.emit(f"  {kept} pass(es) kept, {removed} duplicate(s) removed")

            files  = sorted(f for f in os.listdir(session_dir)
                            if os.path.isfile(os.path.join(session_dir, f)))
            failed = False

            for fname in files:
                fpath        = os.path.join(session_dir, fname)
                content_type = "image/png" if fname.endswith(".png") else "application/json"
                # colons in the session timestamp (e.g. "11:42") break Supabase URL path segments
                remote_path  = f"{session}/{fname}".replace(":", "-")

                self.progress.emit(f"Uploading {remote_path}...")
                try:
                    with open(fpath, "rb") as fh:
                        body = fh.read()

                    resp = requests.post(
                        f"{self._url}/storage/v1/object/{self._bucket}/{remote_path}",
                        headers={
                            "Authorization": f"Bearer {self._key}",
                            "apikey": self._key,
                            "Content-Type": content_type,
                        },
                        data=body,
                        timeout=30,
                    )

                    if resp.status_code not in (200, 201):
                        self.progress.emit(f"  HTTP {resp.status_code}: {resp.text[:160]}")
                        failed = True
                        break

                    total_uploaded += 1

                except Exception as exc:
                    self.progress.emit(f"  {exc}")
                    failed = True
                    break

            # only delete local copy after the entire session uploaded successfully
            if not failed:
                shutil.rmtree(session_dir)
                sessions_ok += 1
                self.progress.emit(f"{session} - uploaded and local copy deleted\n")
            else:
                self.progress.emit(f"{session} - upload incomplete, local files kept\n")

        summary = (
            f"Finished uploading {sessions_ok}/{len(sessions)} session(s).\n"
            f"  - {total_removed} duplicate pass(es) removed before upload\n"
            f"  - {total_uploaded} file(s) sent to Supabase"
        )
        self.finished.emit(True, summary)


class UploadProgressDialog(QDialog):
    """Live-log dialog shown while DebugUploaderThread runs."""

    def __init__(self, supabase_url: str, anon_key: str, bucket: str,
                 debug_dir: str, parent=None):
        super().__init__(parent)
        self._url       = supabase_url
        self._key       = anon_key
        self._bucket    = bucket
        self._debug_dir = debug_dir

        self.setWindowTitle("Upload Debug Data to Cloud")
        self.setFixedSize(560, 400)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setStyleSheet("""
            QDialog  { background-color: #1a252f; color: #ecf0f1; }
            QLabel   { font-size: 13px; color: #ecf0f1; }
            QTextEdit {
                background-color: #0d1b22; color: #a8d8a8;
                font-family: Consolas, monospace; font-size: 11px;
                border: 1px solid #2c3e50; border-radius: 4px;
            }
            QProgressBar {
                border: 1px solid #2c3e50; border-radius: 4px;
                background-color: #0d1b22; height: 14px; text-align: center;
            }
            QProgressBar::chunk { background-color: #16a085; border-radius: 3px; }
            QPushButton {
                background-color: #16a085; color: #ffffff;
                padding: 8px 20px; border: none; border-radius: 4px;
                font-size: 13px; font-weight: bold;
            }
            QPushButton:hover    { background-color: #13856b; }
            QPushButton:disabled { background-color: #3d5a52; color: #7f8c8d; }
        """)
        self._build_ui()
        self._start()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Uploading debug sessions to Supabase Storage...")
        title.setStyleSheet("font-size: 14px; font-weight: bold; color: #f1c40f;")
        layout.addWidget(title)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)

        # indeterminate bar while the thread is running
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        layout.addWidget(self.bar)

        self.close_btn = QPushButton("Close")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        layout.addWidget(self.close_btn, alignment=Qt.AlignRight)

    def _start(self):
        self.thread = DebugUploaderThread(
            self._url, self._key, self._bucket, self._debug_dir
        )
        self.thread.progress.connect(self.log.append)
        self.thread.finished.connect(self._on_done)
        self.thread.start()

    def _on_done(self, _ok, summary):
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self.log.append("\n──────────────────────────────")
        self.log.append(summary)
        self.close_btn.setEnabled(True)


class SignUpWindow(QWidget):
    def __init__(self, sign_in_window):
        super().__init__()
        self.sign_in_window = sign_in_window
        self.setWindowTitle("Sign Up")
        self.setFixedSize(400, 550)
        self.setStyleSheet("""
            QWidget {
                background-color: #1a252f;
                color: #ecf0f1;
            }
            QLabel {
                font-size: 14px;
                color: #ecf0f1;
            }
            QLineEdit, QComboBox {
                padding: 8px;
                border: 1px solid #95a5a6;
                border-radius: 4px;
                font-size: 14px;
                background-color: #ffffff;
                color: #2c3e50;
            }
            QToolButton {
                border: none;
                background: transparent;
                font-size: 16px;
                padding-right: 4px;
            }
            QToolButton:hover {
                color: #f1c40f;
            }
            QPushButton#registerBtn {
                background-color: #27ae60;
                color: #ffffff;
                padding: 10px 16px;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton#registerBtn:hover {
                background-color: #1e8449;
            }
            QPushButton#signInBtn {
                background-color: #e67e22;
                color: #ffffff;
                padding: 10px 16px;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton#signInBtn:hover {
                background-color: #d35400;
            }
            QFrame#separator {
                background-color: #7f8c8d;
                max-height: 1px;
            }
        """)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 20, 30, 20)
        layout.setSpacing(15)

        user_label = QLabel("Username:")
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Enter a new username")
        layout.addWidget(user_label)
        layout.addWidget(self.user_input)

        pass_label = QLabel("Password:")
        self.pass_input = QLineEdit()
        self.pass_input.setPlaceholderText("Choose a password")
        self.pass_input.setEchoMode(QLineEdit.Password)

        # eye button sits inside the password field via a nested layout
        self.eye_btn = QToolButton(self.pass_input)
        self.eye_btn.setText("👁")
        self.eye_btn.setCursor(Qt.PointingHandCursor)
        self.eye_btn.setCheckable(True)
        self.eye_btn.setToolTip("Show / hide password")
        self.eye_btn.clicked.connect(self.toggle_password_visibility)

        self.pass_input.setLayout(QHBoxLayout())
        self.pass_input.layout().setContentsMargins(0, 0, 0, 0)
        self.pass_input.layout().addStretch()
        self.pass_input.layout().addWidget(self.eye_btn)

        layout.addWidget(pass_label)
        layout.addWidget(self.pass_input)

        plan_label = QLabel("Choose your plan:")
        self.plan_combo = QComboBox()
        self.plan_combo.addItem("Select a plan")
        self.plan_combo.addItem("free")
        self.plan_combo.addItem("0.0$ per month")
        self.plan_combo.addItem("why do the plan options even exist")
        layout.addWidget(plan_label)
        layout.addWidget(self.plan_combo)

        # cc field is hidden until the user picks a plan
        cc_label = QLabel("Totally Real Credit Card:")
        self.cc_input = QLineEdit()
        self.cc_input.setPlaceholderText("Enter your credit card number")
        cc_label.hide()
        self.cc_input.hide()
        layout.addWidget(cc_label)
        layout.addWidget(self.cc_input)

        self.register_btn = QPushButton("Register")
        self.register_btn.setObjectName("registerBtn")
        self.register_btn.setCursor(Qt.PointingHandCursor)
        self.register_btn.clicked.connect(self.handle_register)
        layout.addWidget(self.register_btn)

        separator = QFrame()
        separator.setObjectName("separator")
        separator.setFrameShape(QFrame.HLine)
        layout.addWidget(separator)

        back_label = QLabel("Already have an account?")
        back_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(back_label)

        self.back_to_sign_in_btn = QPushButton("Sign In")
        self.back_to_sign_in_btn.setObjectName("signInBtn")
        self.back_to_sign_in_btn.setCursor(Qt.PointingHandCursor)
        self.back_to_sign_in_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.back_to_sign_in_btn.clicked.connect(self.back_to_sign_in)
        layout.addWidget(self.back_to_sign_in_btn)

        layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Expanding))

        self.cc_label = cc_label
        self.plan_combo.currentIndexChanged.connect(self.on_plan_selected)

    def toggle_password_visibility(self, checked: bool):
        if checked:
            self.pass_input.setEchoMode(QLineEdit.Normal)
            self.eye_btn.setText("🔒")
        else:
            self.pass_input.setEchoMode(QLineEdit.Password)
            self.eye_btn.setText("👁")

    def on_plan_selected(self, index):
        # show the cc field only once an actual plan is selected (index 0 is the placeholder)
        if index > 0:
            self.cc_label.show()
            self.cc_input.show()
        else:
            self.cc_label.hide()
            self.cc_input.hide()

    def handle_register(self):
        username   = self.user_input.text().strip()
        password   = self.pass_input.text().strip()
        plan_index = self.plan_combo.currentIndex()
        plan_text  = self.plan_combo.currentText().strip()
        cc_text    = self.cc_input.text().strip()

        if not username:
            QMessageBox.warning(self, "Missing Username", "Please enter a username.")
            return
        if not password:
            QMessageBox.warning(self, "Missing Password", "Please enter a password.")
            return
        if plan_index == 0:
            QMessageBox.warning(self, "Plan Not Selected", "Please choose a valid plan.")
            return
        if not cc_text:
            QMessageBox.warning(self, "Missing Credit Card", "Please enter your credit card number.")
            return

        payload = {
            "username": username,
            "password": password,
            "plan": plan_text,
            "credit_card": cc_text
        }
        plaintext_json = json.dumps(payload)
        encrypted_b64  = encrypt_and_encode(plaintext_json)

        try:
            r = _post_with_retry(
                f"{SERVER_URL}/signup",
                data={"data": encrypted_b64},
                timeout=5,
            )
        except requests.exceptions.RequestException as e:
            QMessageBox.critical(self, "Network Error", f"Could not reach server:\n{e}")
            return

        resp_cipher = r.text.strip()
        decrypted   = decode_and_decrypt(resp_cipher)
        try:
            resp_json = json.loads(decrypted)
        except Exception:
            QMessageBox.warning(self, "Error", f"Unexpected response from server (status {r.status_code})")
            return

        if resp_json.get("success"):
            QMessageBox.information(self, "Success", resp_json.get("message", "Registered"))
            self.close()
            self.sign_in_window.show()
            self.sign_in_window.raise_()
            self.sign_in_window.activateWindow()
        else:
            QMessageBox.warning(self, "Error", resp_json.get("message", "Unknown error"))

    def back_to_sign_in(self):
        self.close()
        self.sign_in_window.show()
        self.sign_in_window.raise_()
        self.sign_in_window.activateWindow()


class SignInWindow(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.setWindowTitle("Sign In")
        self.setFixedSize(400, 380)
        self.setStyleSheet("""
            QWidget {
                background-color: #1a252f;
                color: #ecf0f1;
            }
            QLabel {
                font-size: 14px;
                color: #ecf0f1;
            }
            QLineEdit {
                padding: 8px;
                border: 1px solid #95a5a6;
                border-radius: 4px;
                font-size: 14px;
                background-color: #ffffff;
                color: #2c3e50;
            }
            QToolButton {
                border: none;
                background: transparent;
                font-size: 16px;
                padding-right: 4px;
            }
            QToolButton:hover {
                color: #f1c40f;
            }
            QPushButton#signInBtn {
                background-color: #e67e22;
                color: #ffffff;
                padding: 10px 16px;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton#signInBtn:hover {
                background-color: #d35400;
            }
            QPushButton#createAccountBtn {
                background-color: #8e44ad;
                color: #ffffff;
                padding: 10px 16px;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton#createAccountBtn:hover {
                background-color: #6c3483;
            }
            QFrame#separator {
                background-color: #7f8c8d;
                max-height: 1px;
            }
        """)
        self.sign_up_window = None
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 20, 30, 20)
        layout.setSpacing(15)

        user_label = QLabel("Username:")
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Enter your username")
        layout.addWidget(user_label)
        layout.addWidget(self.user_input)

        pass_label = QLabel("Password:")
        self.pass_input = QLineEdit()
        self.pass_input.setPlaceholderText("Enter your password")
        self.pass_input.setEchoMode(QLineEdit.Password)

        self.eye_btn = QToolButton(self.pass_input)
        self.eye_btn.setText("👁")
        self.eye_btn.setCursor(Qt.PointingHandCursor)
        self.eye_btn.setCheckable(True)
        self.eye_btn.setToolTip("Show / hide password")
        self.eye_btn.clicked.connect(self.toggle_password_visibility)

        self.pass_input.setLayout(QHBoxLayout())
        self.pass_input.layout().setContentsMargins(0, 0, 0, 0)
        self.pass_input.layout().addStretch()
        self.pass_input.layout().addWidget(self.eye_btn)

        layout.addWidget(pass_label)
        layout.addWidget(self.pass_input)

        self.submit_btn = QPushButton("Sign In")
        self.submit_btn.setObjectName("signInBtn")
        self.submit_btn.setCursor(Qt.PointingHandCursor)
        self.submit_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.submit_btn.clicked.connect(self.handle_sign_in)
        layout.addWidget(self.submit_btn)

        separator = QFrame()
        separator.setObjectName("separator")
        separator.setFrameShape(QFrame.HLine)
        layout.addWidget(separator)

        signup_notice = QLabel("Don't have an account?")
        signup_notice.setAlignment(Qt.AlignCenter)
        layout.addWidget(signup_notice)

        self.create_account_btn = QPushButton("Sign Up")
        self.create_account_btn.setObjectName("createAccountBtn")
        self.create_account_btn.setCursor(Qt.PointingHandCursor)
        self.create_account_btn.clicked.connect(self.open_sign_up)
        layout.addWidget(self.create_account_btn)

        layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Expanding))

    def toggle_password_visibility(self, checked: bool):
        if checked:
            self.pass_input.setEchoMode(QLineEdit.Normal)
            self.eye_btn.setText("🔒")
        else:
            self.pass_input.setEchoMode(QLineEdit.Password)
            self.eye_btn.setText("👁")

    def handle_sign_in(self):
        username = self.user_input.text().strip()
        password = self.pass_input.text().strip()

        if not username:
            QMessageBox.warning(self, "Missing Username", "Please enter your username.")
            return
        if not password:
            QMessageBox.warning(self, "Missing Password", "Please enter your password.")
            return

        payload        = {"username": username, "password": password}
        plaintext_json = json.dumps(payload)
        encrypted_b64  = encrypt_and_encode(plaintext_json)

        try:
            r = _post_with_retry(
                f"{SERVER_URL}/login",
                data={"data": encrypted_b64},
                timeout=5,
            )
        except requests.exceptions.RequestException as e:
            QMessageBox.critical(self, "Network Error", f"Could not reach server:\n{e}")
            return

        resp_cipher = r.text.strip()
        decrypted   = decode_and_decrypt(resp_cipher)
        try:
            resp_json = json.loads(decrypted)
        except Exception:
            QMessageBox.warning(self, "Error", f"Unexpected response from server (status {r.status_code})")
            return

        if resp_json.get("success"):
            QMessageBox.information(self, "Success", resp_json.get("message", "Logged in successfully"))
            user_plan = resp_json.get("plan", "")
            user_cc   = resp_json.get("credit_card", "")
            self.main_window.set_user(username, password, user_plan, user_cc)
            self.close()
        else:
            QMessageBox.warning(self, "Error", resp_json.get("message", "Invalid credentials"))

    def open_sign_up(self):
        if self.sign_up_window is None:
            self.sign_up_window = SignUpWindow(self)
        self.hide()
        self.sign_up_window.show()
        self.sign_up_window.raise_()
        self.sign_up_window.activateWindow()


class UserMenuWindow(QWidget):
    def __init__(self, main_window, username, password, plan, credit_card):
        super().__init__()
        self.main_window = main_window
        self.username    = username
        self.password    = password
        self.plan        = plan
        self.credit_card = credit_card

        self.setWindowTitle("User Profile")
        self.setFixedSize(350, 260)
        self.setStyleSheet("""
            QWidget {
                background-color: #2c3e50;
                color: #ecf0f1;
                font-family: Arial;
            }
            QLabel.title {
                font-size: 20px;
                font-weight: bold;
                color: #f1c40f;
            }
            QLabel.field-label {
                font-size: 14px;
                font-weight: bold;
                color: #ecf0f1;
            }
            QLabel.field-value {
                font-size: 14px;
                color: #bdc3c7;
            }
            QFrame.separator {
                background-color: #7f8c8d;
                max-height: 2px;
            }
            QPushButton#logoutBtn {
                background-color: #c0392b;
                color: #ffffff;
                padding: 10px 16px;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton#logoutBtn:hover {
                background-color: #a93226;
            }
        """)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title_label = QLabel("Profile Information")
        title_label.setObjectName("titleLabel")
        title_label.setProperty("class", "title")
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)

        sep = QFrame()
        sep.setObjectName("separator")
        sep.setProperty("class", "separator")
        sep.setFrameShape(QFrame.HLine)
        layout.addWidget(sep)

        user_row = QHBoxLayout()
        lbl_user = QLabel("Username:")
        lbl_user.setProperty("class", "field-label")
        val_user = QLabel(self.username)
        val_user.setProperty("class", "field-value")
        user_row.addWidget(lbl_user)
        user_row.addSpacing(10)
        user_row.addWidget(val_user)
        user_row.addStretch()
        layout.addLayout(user_row)

        plan_row = QHBoxLayout()
        lbl_plan = QLabel("Plan:")
        lbl_plan.setProperty("class", "field-label")
        val_plan = QLabel(self.plan)
        val_plan.setProperty("class", "field-value")
        plan_row.addWidget(lbl_plan)
        plan_row.addSpacing(10)
        plan_row.addWidget(val_plan)
        plan_row.addStretch()
        layout.addLayout(plan_row)

        cc_row = QHBoxLayout()
        lbl_cc = QLabel("Credit Card:")
        lbl_cc.setProperty("class", "field-label")
        val_cc = QLabel(self.credit_card)
        val_cc.setProperty("class", "field-value")
        cc_row.addWidget(lbl_cc)
        cc_row.addSpacing(10)
        cc_row.addWidget(val_cc)
        cc_row.addStretch()
        layout.addLayout(cc_row)

        layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Minimum, QSizePolicy.Expanding))

        self.logout_btn = QPushButton("Log Out")
        self.logout_btn.setObjectName("logoutBtn")
        self.logout_btn.setCursor(Qt.PointingHandCursor)
        self.logout_btn.clicked.connect(self.handle_logout)
        layout.addWidget(self.logout_btn)

    def handle_logout(self):
        self.main_window.clear_user()
        self.close()


class VpnChoiceWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Choose VPN Mode")
        self.setFixedSize(300, 320)
        self.setStyleSheet("""
            QWidget {
                background-color: #1a252f;
                color: #ecf0f1;
            }
            QPushButton {
                background-color: #16a085;
                color: #ffffff;
                padding: 10px;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #13856b;
            }
            QCheckBox {
                font-size: 13px;
                color: #a6adc8;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 3px;
                border: 2px solid #7f8c8d;
                background-color: #1a252f;
            }
            QCheckBox::indicator:checked {
                background-color: #a6e3a1;
                border: 2px solid #a6e3a1;
            }
        """)
        self.ad_block_enabled = False
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        label = QLabel("Select VPN Mode:")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("font-size: 16px; color: #ecf0f1;")
        layout.addWidget(label)

        self.regular_btn = QPushButton("Regular Secured VPN")
        self.regular_btn.clicked.connect(self.launch_regular)
        layout.addWidget(self.regular_btn)

        self.firewall_btn = QPushButton("Firewall-Enabled VPN")
        self.firewall_btn.clicked.connect(self.launch_firewall)
        layout.addWidget(self.firewall_btn)

        self.noproxy_btn = QPushButton("No Proxy")
        self.noproxy_btn.setStyleSheet("""
            QPushButton {
                background-color: #7f8c8d;
                color: #ffffff;
                padding: 10px;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #636e72;
            }
        """)
        self.noproxy_btn.clicked.connect(self.launch_noproxy)
        layout.addWidget(self.noproxy_btn)

        self.adblock_checkbox = QCheckBox("Enable ML Ad Blocker")
        self.adblock_checkbox.stateChanged.connect(self.toggle_adblock)
        layout.addWidget(self.adblock_checkbox)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background-color: #2c3e50; max-height: 1px;")
        layout.addWidget(sep)

        self.upload_btn = QPushButton("Upload Debug Data")
        self.upload_btn.setStyleSheet("""
            QPushButton {
                background-color: #2980b9;
                color: #ffffff;
                padding: 10px;
                border: none;
                border-radius: 4px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #1f618d; }
        """)
        self.upload_btn.setToolTip(
            "Deduplicate and upload all debug sessions to Supabase,\n"
            "then delete local copies."
        )
        self.upload_btn.clicked.connect(self.open_upload_dialog)
        layout.addWidget(self.upload_btn)

        layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Expanding))

    def toggle_adblock(self, state):
        self.ad_block_enabled = (state == Qt.Checked)

    def open_upload_dialog(self):
        if SUPABASE_URL.startswith("https://YOUR_"):
            QMessageBox.warning(
                self,
                "Not Configured",
                "Please set SUPABASE_URL and SUPABASE_ANON_KEY\n"
                "in main_client.py before uploading.",
            )
            return
        dlg = UploadProgressDialog(
            supabase_url=SUPABASE_URL,
            anon_key=SUPABASE_ANON_KEY,
            bucket=SUPABASE_BUCKET,
            debug_dir=DEBUG_OUTPUT_DIR,
            parent=self,
        )
        dlg.exec_()

    def launch_noproxy(self):
        cmd = [sys.executable, "regular_browser.py", "--no-proxy"]
        if self.ad_block_enabled:
            cmd.append("--ad-block")
        subprocess.Popen(cmd)
        self.close()

    def launch_regular(self):
        cmd = [sys.executable, "regular_browser.py"]
        if self.ad_block_enabled:
            cmd.append("--ad-block")
        subprocess.Popen(cmd)
        self.close()

    def launch_firewall(self):
        cmd = [sys.executable, "mitm_browser.py"]
        if self.ad_block_enabled:
            cmd.append("--ad-block")
        subprocess.Popen(cmd)
        self.close()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MSVPN")
        self.setFixedSize(800, 600)
        self.setStyleSheet("""
            QWidget {
                background-color: #1a252f;
            }
        """)

        self.sign_in_window = None
        self.choice_window  = None

        # current session data — cleared on logout
        self.current_username    = None
        self.current_password    = None
        self.current_plan        = None
        self.current_credit_card = None

        self.init_ui()

    def init_ui(self):
        top_container = QWidget(self)
        top_container.setFixedHeight(220)
        top_layout = QVBoxLayout(top_container)
        top_layout.setContentsMargins(20, 30, 20, 0)
        top_layout.setSpacing(5)

        sign_in_row = QHBoxLayout()
        sign_in_row.setContentsMargins(0, 0, 0, 0)
        sign_in_row.setSpacing(0)
        sign_in_row.addSpacerItem(QSpacerItem(40, 10, QSizePolicy.Expanding, QSizePolicy.Minimum))

        self.sign_in_btn = QPushButton("Sign in to unlock greater protection")
        self.sign_in_btn.setCursor(Qt.PointingHandCursor)
        self.sign_in_btn.setFixedHeight(36)
        self.sign_in_btn.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c;
                color: #ffffff;
                padding: 6px 16px;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #c0392b;
            }
        """)
        self.sign_in_btn.clicked.connect(self.open_sign_in)
        sign_in_row.addWidget(self.sign_in_btn)

        # shown instead of sign_in_btn once the user is logged in
        self.user_btn = QPushButton()
        self.user_btn.setVisible(False)
        self.user_btn.setCursor(Qt.PointingHandCursor)
        self.user_btn.setFixedHeight(36)
        self.user_btn.setStyleSheet("""
            QPushButton {
                background-color: #2980b9;
                color: #ffffff;
                padding: 6px 16px;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1f618d;
            }
        """)
        self.user_btn.clicked.connect(self.open_user_menu)
        sign_in_row.addWidget(self.user_btn)

        self.title_label = QLabel("msvpn")
        title_font = QFont("Arial", 56, QFont.Bold)
        self.title_label.setFont(title_font)
        self.title_label.setStyleSheet("color: #f1c40f;")
        self.title_label.setAlignment(Qt.AlignCenter)

        self.subtitle_label = QLabel("man in the middle secured vpn")
        subtitle_font = QFont("Arial", 18)
        self.subtitle_label.setFont(subtitle_font)
        self.subtitle_label.setStyleSheet("color: #f1c40f;")
        self.subtitle_label.setAlignment(Qt.AlignCenter)

        top_layout.addLayout(sign_in_row)
        top_layout.addStretch(1)
        top_layout.addWidget(self.title_label)
        top_layout.addSpacing(10)
        top_layout.addWidget(self.subtitle_label)
        top_layout.addStretch(1)

        central_layout = QVBoxLayout()
        central_layout.setAlignment(Qt.AlignCenter)

        self.vpn_btn = QPushButton()
        self.vpn_btn.setFixedSize(240, 240)
        self.vpn_btn.setCursor(Qt.PointingHandCursor)

        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(20)
        shadow.setXOffset(0)
        shadow.setYOffset(5)
        shadow.setColor(QColor(0, 0, 0, 160))
        self.vpn_btn.setGraphicsEffect(shadow)

        icon_pixmap = QPixmap("actv_vpn.png").scaled(220, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.vpn_btn.setIcon(QIcon(icon_pixmap))
        self.vpn_btn.setIconSize(icon_pixmap.rect().size())
        self.vpn_btn.setStyleSheet(
            "QPushButton { background-color: #2c3e50; border: 4px solid #16a085; border-radius: 120px;}"
            "QPushButton:hover { background-color: #34495e;}"
        )
        self.vpn_btn.setToolTip("Click to activate VPN")
        self.vpn_btn.clicked.connect(self.handle_vpn_activation)
        central_layout.addWidget(self.vpn_btn)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 20)
        main_layout.setSpacing(0)

        main_layout.addWidget(top_container)
        main_layout.addStretch(2)
        main_layout.addLayout(central_layout)
        main_layout.addStretch(1)

        self.setLayout(main_layout)

    def open_sign_in(self):
        if self.sign_in_window is None:
            self.sign_in_window = SignInWindow(self)
        self.sign_in_window.show()
        self.sign_in_window.raise_()
        self.sign_in_window.activateWindow()

    def handle_vpn_activation(self):
        # unauthenticated users get a basic browser with no VPN choice
        if self.current_username:
            if self.choice_window is None:
                self.choice_window = VpnChoiceWindow()
            self.choice_window.show()
        else:
            subprocess.Popen([sys.executable, "regular_browser.py"])

    def set_user(self, username: str, password: str, plan: str, credit_card: str):
        self.current_username    = username
        self.current_password    = password
        self.current_plan        = plan
        self.current_credit_card = credit_card

        self.sign_in_btn.setVisible(False)
        self.user_btn.setText(username)
        self.user_btn.setVisible(True)

    def clear_user(self):
        self.current_username    = None
        self.current_password    = None
        self.current_plan        = None
        self.current_credit_card = None

        self.user_btn.setVisible(False)
        self.sign_in_btn.setVisible(True)

    def open_user_menu(self):
        if not self.current_username:
            return

        self.user_menu_window = UserMenuWindow(
            main_window=self,
            username=self.current_username,
            password=self.current_password,
            plan=self.current_plan,
            credit_card=self.current_credit_card
        )
        self.user_menu_window.show()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
