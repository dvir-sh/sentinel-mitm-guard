# Secure Browsing Suite - VPN + MITM Threat Filtering + Visual Ad Blocker

A security system that combines a **VPN-style IP-masking proxy**, a **MITM-based real-time threat scanner**, and a **Visual AI ad blocker**, wrapped in a fully automated client/server pipeline. Built for both individual users who want private, ad-free, malware-safe browsing, and for organizations that need to monitor and restrict employee internet traffic.

## Features

- **IP masking / VPN-style proxy** - routes traffic through a remote proxy server to hide the user's real IP.
- **Automatic CA certificate provisioning** for the MITM browser, with retry logic and no manual user setup.
- **Two scanning options**
  - *Client-side scan*: checks visited URLs against VirusTotal and warns/closes malicious tabs.
  - *MITM scan*: intercepts HTTPS traffic before it reaches the browser, blocking malicious domains and downloaded files at the proxy level.
- **Visual AI ad blocker** - uses YOLOv8 for on-screen element detection plus a trained Keras classifier to identify and hide ads directly in the DOM, independent of static blocklists.

## Architecture

| Component | Role |
|---|---|
| `main_client` | Main GUI - launches browsers, toggles ad-block, manages login |
| `server_main_connects.py` | Auth server (Flask + SQLite + PBKDF2) |
| `regular_proxy_server.py` | Plain IP-masking relay proxy |
| `mitm_proxy_server.py` | MITM proxy - domain/file scanning via VirusTotal, CA issuance |
| `regular_browser.py` | Browser with tab-polling malware URL checks |
| `mitm_browser.py` | Browser routed through the MITM proxy, auto-installs CA |
| `visual_ml_potential_ads.py` | YOLO-based ad element detection + geometric pre-filtering |
| `ad_blocker.py` | Background thread combining detection + Keras classification + DOM hiding |

**Flow:** the client authenticates with the auth server, then launches either the regular or MITM browser (optionally with the ad blocker as a background thread). The MITM browser fetches a CA cert from the MITM proxy to transparently decrypt and scan HTTPS traffic before it reaches the user.

## Tech Stack

- **Language:** Python, SQL
- **Key libraries:** mitmproxy, Selenium, PyQt5, TensorFlow/Keras, Ultralytics YOLOv8, OpenCV, Flask
- **External services:** VirusTotal API v3, Supabase (debug data upload)
- **OS:** Windows
- **Deployment:** Railway (VPS) + GitHub for CI deployment

