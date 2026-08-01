import logging
import asyncio
import itertools
import os
import signal
import time
import requests
import ipaddress
import re
import hashlib
from urllib.parse import urlparse

from mitmproxy import http
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

MITM_PORT = 8443
CA_PATH   = os.path.expanduser("~/.mitmproxy/mitmproxy-ca-cert.pem")

VT_API_KEYS = os.environ["VT_API_KEYS"].split(",")
_key_cycle  = itertools.cycle(VT_API_KEYS)

# semaphores keep concurrent VT requests at most equal to the number of API keys
file_scan_semaphore   = asyncio.Semaphore(len(VT_API_KEYS))
domain_check_semaphore = asyncio.Semaphore(len(VT_API_KEYS))

_domain_cache      = {}
_cache_timestamps  = {}
CACHE_TTL          = 3600

_file_cache            = {}
_file_cache_timestamps = {}
FILE_CACHE_TTL         = 3600

BLOCK_MALICIOUS = True

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mitmproxy")


def get_vt_api_key() -> str:
    return next(_key_cycle)


def is_private_or_localhost(hostname: str) -> bool:
    hn = hostname.lower().split(":", 1)[0]
    if hn == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(hn)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


async def is_domain_malicious(domain: str) -> bool:
    domain_to_check = domain.lower().split(":", 1)[0]

    if is_private_or_localhost(domain_to_check):
        return False

    now = time.time()
    if domain_to_check in _domain_cache:
        if now - _cache_timestamps.get(domain_to_check, 0) < CACHE_TTL:
            return _domain_cache[domain_to_check]

    async with domain_check_semaphore:
        api_key = get_vt_api_key()
        headers = {"x-apikey": api_key}
        url     = f"https://www.virustotal.com/api/v3/domains/{domain_to_check}"
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: requests.get(url, headers=headers, timeout=10)
            )
            resp.raise_for_status()
            data      = resp.json().get("data", {})
            stats     = data.get("attributes", {}).get("last_analysis_stats", {})
            malicious = stats.get("malicious", 0) > 0
            _domain_cache[domain_to_check]     = malicious
            _cache_timestamps[domain_to_check] = now
            return malicious
        except Exception as e:
            logger.warning(f"Error checking domain {domain_to_check}: {e}")
            return False


async def is_file_malicious(content_bytes: bytes) -> bool:
    normalized = content_bytes.strip()
    sha256     = hashlib.sha256(normalized).hexdigest()
    now        = time.time()

    if sha256 in _file_cache:
        if now - _file_cache_timestamps.get(sha256, 0) < FILE_CACHE_TTL:
            return _file_cache[sha256]

    async with file_scan_semaphore:
        api_key = get_vt_api_key()
        headers = {"x-apikey": api_key}

        # check if VT already has a report for this hash before uploading
        report_url = f"https://www.virustotal.com/api/v3/files/{sha256}"
        try:
            report_resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: requests.get(report_url, headers=headers, timeout=10)
            )
            if report_resp.status_code == 200:
                data      = report_resp.json().get("data", {})
                stats     = data.get("attributes", {}).get("last_analysis_stats", {})
                malicious = stats.get("malicious", 0) > 0
                _file_cache[sha256]            = malicious
                _file_cache_timestamps[sha256] = now
                return malicious
            if report_resp.status_code not in (200, 404):
                logger.warning(f"Unexpected status {report_resp.status_code} on report lookup; falling back.")
        except Exception as e:
            logger.warning(f"Report lookup error for {sha256[:10]}...: {e}")

        # no existing report — upload the file and wait for analysis
        try:
            files      = {"file": ("file", normalized)}
            upload_resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: requests.post(
                    "https://www.virustotal.com/api/v3/files",
                    headers=headers,
                    files=files,
                    timeout=30
                )
            )
            upload_resp.raise_for_status()
            analysis_id = upload_resp.json()["data"]["id"]
        except Exception as e:
            logger.warning(f"File upload error: {e}")
            _file_cache[sha256]            = False
            _file_cache_timestamps[sha256] = now
            return False

    vt_ana_url = f"https://www.virustotal.com/api/v3/analyses/{analysis_id}"
    while True:
        try:
            await asyncio.sleep(2)
            headers = {"x-apikey": api_key}
            r = await asyncio.get_event_loop().run_in_executor(
                None, lambda: requests.get(vt_ana_url, headers=headers, timeout=10)
            )
            r.raise_for_status()
            j      = r.json()
            status = j.get("data", {}).get("attributes", {}).get("status")
            if status == "queued":
                continue
            if status == "completed":
                stats     = j.get("data", {}).get("attributes", {}).get("stats", {})
                malicious = stats.get("malicious", 0) > 0
                _file_cache[sha256]            = malicious
                _file_cache_timestamps[sha256] = now
                return malicious
            _file_cache[sha256]            = False
            _file_cache_timestamps[sha256] = now
            return False
        except Exception as e:
            logger.warning(f"Polling error for {analysis_id}: {e}")
            _file_cache[sha256]            = False
            _file_cache_timestamps[sha256] = now
            return False


class MSVPNProxy:
    async def request(self, flow: http.HTTPFlow):
        url    = flow.request.pretty_url
        parsed = urlparse(url)
        domain = parsed.netloc.lower().split(":", 1)[0]

        # serve the CA cert directly so clients can install it without a separate step
        if flow.request.path == "/mitmproxy-ca-cert.pem":
            if not os.path.isfile(CA_PATH):
                flow.response = http.Response.make(
                    404, b"CA not found", {"Content-Type": "text/plain"}
                )
                return
            with open(CA_PATH, "rb") as f:
                cert_bytes = f.read()
            flow.response = http.Response.make(
                200,
                cert_bytes,
                {
                    "Content-Type": "application/x-pem-file",
                    "Content-Disposition": "attachment; filename=mitmproxy-ca-cert.pem",
                }
            )
            return

        malicious_domain = await is_domain_malicious(domain)
        if BLOCK_MALICIOUS and malicious_domain:
            flow.response = http.Response.make(
                403,
                b"<h1>403 Forbidden</h1><p><b>Blocked by MSVPN: malicious domain</b></p>",
                {"Content-Type": "text/html"}
            )

    async def response(self, flow: http.HTTPFlow):
        if flow.response is None:
            return

        url    = flow.request.pretty_url
        parsed = urlparse(url)
        path   = parsed.path.lower()
        query  = parsed.query.lower()

        if len(flow.response.raw_content) < 10:
            return

        content_disp = flow.response.headers.get("Content-Disposition", "").lower()

        is_download = any([
            "attachment" in content_disp,
            "download" in query,
            re.search(r'\.(exe|dll|zip|rar|pdf|docx?|xlsx?|pptx?|jar|txt)$', path),
            "mms-type" in query
        ])

        ctype     = flow.response.headers.get("Content-Type", "").lower()
        is_binary = any(term in ctype for term in [
            "octet-stream", "pdf", "zip", "x-msdownload",
            "vnd.microsoft.portable-executable", "video", "image"
        ])

        if is_download or is_binary:
            try:
                malicious_file = await is_file_malicious(flow.response.raw_content)
                if malicious_file:
                    flow.response = http.Response.make(
                        403,
                        b"<h1>403 Forbidden</h1><p><b>Blocked by MSVPN: malicious file download</b></p>",
                        {"Content-Type": "text/html"}
                    )
            except Exception as e:
                logger.error(f"File scan failed: {e}")


async def run_proxy():
    loop = asyncio.get_event_loop()
    opts = Options(
        listen_host="0.0.0.0",
        listen_port=MITM_PORT,
        ssl_insecure=True
    )
    m = DumpMaster(opts)
    m.addons.add(MSVPNProxy())
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(m.shutdown()))
    await m.run()


if __name__ == "__main__":
    asyncio.run(run_proxy())
