import os
import sqlite3
import hashlib
import hmac
import binascii
import json
import base64
import threading
from flask import Flask, request, make_response

DB_FILENAME       = "users.db"
PBKDF2_ITERATIONS = 100_000
HASH_NAME         = "sha256"

# pulled from env so the key never lives in source
SHARED_KEY = os.environ.get("SHARED_SECRET")
if SHARED_KEY is None:
    raise RuntimeError("You must set SHARED_SECRET in the environment!")

db_lock = threading.Lock()


def generate_salt(length: int = 16) -> bytes:
    return os.urandom(length)

def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(HASH_NAME, password.encode("utf-8"), salt, PBKDF2_ITERATIONS)

def verify_password(stored_hash_hex: str, stored_salt_hex: str, provided_password: str) -> bool:
    salt          = binascii.unhexlify(stored_salt_hex)
    expected_hash = binascii.unhexlify(stored_hash_hex)
    provided_hash = hash_password(provided_password, salt)
    # compare_digest prevents timing attacks
    return hmac.compare_digest(expected_hash, provided_hash)


# simple XOR stream cipher — security comes from keeping SHARED_KEY secret
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
    return base64.b64encode(xored).decode("ascii")

def decode_and_decrypt(cipher_b64: str) -> str:
    try:
        xored     = base64.b64decode(cipher_b64)
        key_bytes = SHARED_KEY.encode("utf-8")
        raw       = _xor_bytes(xored, key_bytes)
        return raw.decode("utf-8")
    except Exception:
        return ""


def init_db():
    conn   = sqlite3.connect(DB_FILENAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            salt          TEXT NOT NULL,
            plan          TEXT NOT NULL,
            credit_card   TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

app = Flask(__name__)
init_db()


@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


@app.route("/signup", methods=["POST"])
def signup():
    cipher_b64 = request.form.get("data", "")
    if not cipher_b64:
        return _encrypted_response({"success": False, "message": "No data provided"}, 400)

    plaintext = decode_and_decrypt(cipher_b64)
    if not plaintext:
        return _encrypted_response({"success": False, "message": "Could not decrypt payload"}, 400)

    try:
        payload = json.loads(plaintext)
    except Exception:
        return _encrypted_response({"success": False, "message": "Invalid JSON"}, 400)

    username    = payload.get("username", "").strip()
    password    = payload.get("password", "").strip()
    plan        = payload.get("plan", "").strip()
    credit_card = payload.get("credit_card", "").strip()

    if not username:
        return _encrypted_response({"success": False, "message": "Username is required"}, 400)
    if not password:
        return _encrypted_response({"success": False, "message": "Password is required"}, 400)

    valid_plans = {"free", "0.0$ per month", "why does the plan options even exist"}
    if plan not in valid_plans:
        return _encrypted_response({"success": False, "message": "Invalid plan selected"}, 400)
    if not credit_card:
        return _encrypted_response({"success": False, "message": "Credit card is required"}, 400)

    salt_bytes = generate_salt()
    hash_bytes = hash_password(password, salt_bytes)
    salt_hex   = binascii.hexlify(salt_bytes).decode("ascii")
    hash_hex   = binascii.hexlify(hash_bytes).decode("ascii")

    try:
        with db_lock:
            conn   = sqlite3.connect(DB_FILENAME, check_same_thread=False)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (username, password_hash, salt, plan, credit_card)
                VALUES (?, ?, ?, ?, ?)
            """, (username, hash_hex, salt_hex, plan, credit_card))
            conn.commit()
            conn.close()
    except sqlite3.IntegrityError:
        return _encrypted_response({"success": False, "message": "Username already exists"}, 409)
    except Exception as e:
        return _encrypted_response({"success": False, "message": f"Database error: {e}"}, 500)

    return _encrypted_response({"success": True, "message": "Registered successfully"}, 201)


@app.route("/login", methods=["POST"])
def login():
    cipher_b64 = request.form.get("data", "")
    if not cipher_b64:
        return _encrypted_response({"success": False, "message": "No data provided"}, 400)

    plaintext = decode_and_decrypt(cipher_b64)
    if not plaintext:
        return _encrypted_response({"success": False, "message": "Could not decrypt payload"}, 400)

    try:
        payload = json.loads(plaintext)
    except Exception:
        return _encrypted_response({"success": False, "message": "Invalid JSON"}, 400)

    username = payload.get("username", "").strip()
    password = payload.get("password", "").strip()

    if not username:
        return _encrypted_response({"success": False, "message": "Username is required"}, 400)
    if not password:
        return _encrypted_response({"success": False, "message": "Password is required"}, 400)

    try:
        with db_lock:
            conn   = sqlite3.connect(DB_FILENAME, check_same_thread=False)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT password_hash, salt, plan, credit_card
                  FROM users
                 WHERE username = ?
            """, (username,))
            row = cursor.fetchone()
            conn.close()
    except Exception as e:
        return _encrypted_response({"success": False, "message": f"Database error: {e}"}, 500)

    # return the same generic message for both "user not found" and "wrong password"
    # so we don't leak which one it was
    if row is None:
        return _encrypted_response({"success": False, "message": "Invalid username or password"}, 401)

    stored_hash_hex, stored_salt_hex, user_plan, user_cc = row
    if not verify_password(stored_hash_hex, stored_salt_hex, password):
        return _encrypted_response({"success": False, "message": "Invalid username or password"}, 401)

    return _encrypted_response({
        "success": True,
        "message": "Login successful",
        "plan": user_plan,
        "credit_card": user_cc
    }, 200)


def _encrypted_response(payload_dict, http_status):
    plaintext_json = json.dumps(payload_dict)
    encrypted_b64  = encrypt_and_encode(plaintext_json)
    response       = make_response(encrypted_b64, http_status)
    response.headers["Content-Type"] = "text/plain"
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
