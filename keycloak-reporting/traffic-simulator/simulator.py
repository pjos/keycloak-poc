#!/usr/bin/env python3
"""
Keycloak Traffic Simulator

Simulates realistic authentication traffic against Keycloak realms provisioned
by the dataset-loader. For each realm, it exercises:

  - User flows (authorization_code + PKCE S256):
      1. GET  /auth            → login page (extract form action)
      2. POST login form       → 302 redirect with authorization code
      3. POST /token           → exchange code for tokens  [includes code_verifier]
      4. POST /token           → refresh access token
      5. POST /logout          → revoke session

  - Client flows (client_credentials):
      1. POST /token           → client_credentials grant
      2. POST /token           → refresh token (if server issues one)

Environment variables:
  KEYCLOAK_URL            Keycloak base URL               (default: https://keycloak:8443)
  KEYCLOAK_BASE_PATH      Keycloak base path              (default: /auth)
  KEYCLOAK_HOSTNAME       External hostname for URIs      (default: localhost.idyatech.fr)
  REALM_COUNT             Number of realms                (default: 2)
  CLIENTS_PER_REALM       Number of clients per realm     (default: 5)
  USERS_PER_REALM         Number of users per realm       (default: 10)
  REALM_PREFIX            Realm name prefix               (default: realm-)
  LOGIN_CLIENT_ID         Client used for user login      (default: account)
  REDIRECT_URI            Override redirect URI template  (optional)
  USER_NAME_PATTERN       Username format, {i}=index      (default: user-{i})
  USER_PASS_PATTERN       Password format, {i}=index      (default: user-{i}-password)
  CLIENT_ID_PATTERN       Client ID format, {i}=index     (default: client-{i})
  CLIENT_SECRET_PATTERN   Client secret format, {i}=index (default: client-{i}-secret)
  USER_INDEX_GLOBAL       Use global user index across realms
                          (realm_idx * USERS_PER_REALM + local_idx)
                          Set to true if the dataset provider uses a global counter.
                          (default: false — per-realm 0-based index)
  VERIFY_SSL              Verify SSL certificates         (default: false)
  LOOP                    Run continuously                (default: true)
  LOOP_COUNT              Iterations: 0=infinite          (default: 0)
  LOOP_DELAY              Seconds between loops           (default: 30)
  REQUEST_DELAY           Seconds between requests        (default: 0.1)
  CONCURRENCY             Concurrent threads per realm    (default: 4)
"""

import os
import sys
import signal
import time
import logging
import random
import hashlib
import base64
import threading
import urllib3
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KEYCLOAK_URL          = os.getenv("KEYCLOAK_URL", "https://keycloak:8443")
KEYCLOAK_BASE_PATH    = os.getenv("KEYCLOAK_BASE_PATH", "/auth")
KEYCLOAK_HOSTNAME     = os.getenv("KEYCLOAK_HOSTNAME", "localhost.idyatech.fr")
REALM_COUNT           = int(os.getenv("REALM_COUNT", "2"))
CLIENTS_PER_REALM     = int(os.getenv("CLIENTS_PER_REALM", "5"))
USERS_PER_REALM       = int(os.getenv("USERS_PER_REALM", "10"))
REALM_PREFIX          = os.getenv("REALM_PREFIX", "realm-")
LOGIN_CLIENT_ID       = os.getenv("LOGIN_CLIENT_ID", "account")
VERIFY_SSL            = os.getenv("VERIFY_SSL", "false").lower() in ("true", "1", "yes")
LOOP                  = os.getenv("LOOP", "true").lower() in ("true", "1", "yes")
LOOP_COUNT            = int(os.getenv("LOOP_COUNT", "0"))    # 0 = infinite
LOOP_DELAY            = int(os.getenv("LOOP_DELAY", "30"))
REQUEST_DELAY         = float(os.getenv("REQUEST_DELAY", "0.1"))
CONCURRENCY           = int(os.getenv("CONCURRENCY", "4"))

# Naming patterns — {i} is replaced by the computed index
USER_NAME_PATTERN     = os.getenv("USER_NAME_PATTERN",     "user-{i}")
USER_PASS_PATTERN     = os.getenv("USER_PASS_PATTERN",     "user-{i}-password")
CLIENT_ID_PATTERN     = os.getenv("CLIENT_ID_PATTERN",     "client-{i}")
CLIENT_SECRET_PATTERN = os.getenv("CLIENT_SECRET_PATTERN", "client-{i}-secret")

# If true: user index = realm_index * USERS_PER_REALM + local_index
# This matches dataset providers that use a global sequential counter across realms.
USER_INDEX_GLOBAL     = os.getenv("USER_INDEX_GLOBAL", "false").lower() in ("true", "1", "yes")

# Redirect URI template used for the authorization_code flow
_DEFAULT_REDIRECT_TMPL = (
    f"https://{KEYCLOAK_HOSTNAME}:8443{KEYCLOAK_BASE_PATH}/realms/{{realm}}/account"
)
REDIRECT_URI_TMPL = os.getenv("REDIRECT_URI", _DEFAULT_REDIRECT_TMPL)

BASE_URL = f"{KEYCLOAK_URL}{KEYCLOAK_BASE_PATH}"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("traffic-simulator")

if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Thread-safe statistics
# ---------------------------------------------------------------------------

_stats_lock = threading.Lock()
stats = {
    "user_logins":             0,
    "user_login_failures":     0,
    "code_exchanges":          0,
    "code_exchange_failures":  0,
    "user_refreshes":          0,
    "user_refresh_failures":   0,
    "user_logouts":            0,
    "user_logout_failures":    0,
    "client_tokens":           0,
    "client_token_failures":   0,
    "client_refreshes":        0,
    "client_refresh_failures": 0,
}


def _inc(key: str, amount: int = 1) -> None:
    """Thread-safe stats increment."""
    with _stats_lock:
        stats[key] += amount


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

shutdown_requested = False


def _request_shutdown(signum, frame):
    global shutdown_requested
    log.info(f"Received {signal.Signals(signum).name} — shutting down...")
    shutdown_requested = True


signal.signal(signal.SIGTERM, _request_shutdown)
signal.signal(signal.SIGINT,  _request_shutdown)

# ---------------------------------------------------------------------------
# Keycloak URL helpers
# ---------------------------------------------------------------------------


def _token_url(realm: str) -> str:
    return f"{BASE_URL}/realms/{realm}/protocol/openid-connect/token"


def _auth_url(realm: str) -> str:
    return f"{BASE_URL}/realms/{realm}/protocol/openid-connect/auth"


def _logout_url(realm: str) -> str:
    return f"{BASE_URL}/realms/{realm}/protocol/openid-connect/logout"


def _redirect_uri(realm: str) -> str:
    return REDIRECT_URI_TMPL.format(realm=realm)


# ---------------------------------------------------------------------------
# PKCE helpers  (RFC 7636 — S256 method)
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    code_verifier  = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    digest         = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


# ---------------------------------------------------------------------------
# IP / User-Agent generation (deterministic per realm + slot)
# ---------------------------------------------------------------------------

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Chrome/131.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 18_1) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36 OPR/116.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
]


def _ip(realm_idx: int, slot: int) -> str:
    """Deterministic fake source IP: 10.<realm+1>.<slot//256>.<slot%256+1>"""
    octet2 = (realm_idx + 1) % 256
    octet3 = (slot // 256) % 256
    octet4 = min((slot % 256) + 1, 254)
    return f"10.{octet2}.{octet3}.{octet4}"


def _ua(realm_idx: int, slot: int) -> str:
    return _USER_AGENTS[(realm_idx * 31 + slot) % len(_USER_AGENTS)]


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------


def _user_index(realm_idx: int, local_idx: int) -> int:
    """
    Return the effective numeric index used in username/password patterns.
    - USER_INDEX_GLOBAL=false (default): per-realm  → local_idx
    - USER_INDEX_GLOBAL=true            : global     → realm_idx * USERS_PER_REALM + local_idx
    """
    if USER_INDEX_GLOBAL:
        return realm_idx * USERS_PER_REALM + local_idx
    return local_idx


def _username(realm_idx: int, local_idx: int) -> str:
    return USER_NAME_PATTERN.replace("{i}", str(_user_index(realm_idx, local_idx)))


def _password(realm_idx: int, local_idx: int) -> str:
    return USER_PASS_PATTERN.replace("{i}", str(_user_index(realm_idx, local_idx)))


def _client_id(local_idx: int) -> str:
    return CLIENT_ID_PATTERN.replace("{i}", str(local_idx))


def _client_secret(local_idx: int) -> str:
    return CLIENT_SECRET_PATTERN.replace("{i}", str(local_idx))


# ---------------------------------------------------------------------------
# Wait helpers
# ---------------------------------------------------------------------------


def wait_for_keycloak(timeout: int = 300) -> None:
    log.info(f"Waiting for Keycloak at {KEYCLOAK_URL} ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if shutdown_requested:
            sys.exit(0)
        try:
            r = requests.get(
                f"{BASE_URL}/realms/master/.well-known/openid-configuration",
                verify=VERIFY_SSL, timeout=5,
            )
            if r.status_code == 200:
                log.info("Keycloak is ready.")
                return
        except requests.ConnectionError:
            pass
        time.sleep(3)
    log.error(f"Keycloak not reachable after {timeout}s — aborting.")
    sys.exit(1)


def wait_for_realms(timeout: int = 600) -> None:
    log.info(f"Waiting for {REALM_COUNT} realm(s) to be available...")
    deadline = time.time() + timeout
    for i in range(REALM_COUNT):
        realm = f"{REALM_PREFIX}{i}"
        url   = f"{BASE_URL}/realms/{realm}/.well-known/openid-configuration"
        while time.time() < deadline:
            if shutdown_requested:
                return
            try:
                r = requests.get(url, verify=VERIFY_SSL, timeout=5)
                if r.status_code == 200:
                    log.info(f"  ✅ {realm} is ready")
                    break
            except requests.ConnectionError:
                pass
            time.sleep(3)
        else:
            log.error(f"  ❌ {realm} not available after {timeout}s — skipping")
    log.info("All realms are ready.")


def _delay() -> None:
    if REQUEST_DELAY > 0:
        time.sleep(REQUEST_DELAY + random.uniform(0, REQUEST_DELAY))


# ---------------------------------------------------------------------------
# User Flow: Authorization Code Grant  (PKCE S256)
# ---------------------------------------------------------------------------


def user_login_flow(realm: str, realm_idx: int, local_idx: int) -> None:
    """
    Full user authentication flow with PKCE S256:
      1. GET  /auth            → login page (cookies + form action URL)
      2. POST login form       → 302 redirect with authorization code
      3. POST /token           → exchange code for tokens  (code_verifier required)
      4. POST /token           → refresh access token
      5. POST /logout          → revoke session
    """
    if shutdown_requested:
        return

    username = _username(realm_idx, local_idx)
    password = _password(realm_idx, local_idx)
    user_ip  = _ip(realm_idx, local_idx)
    user_ua  = _ua(realm_idx, local_idx)

    log.info(f"  [{realm}] {username} (ip={user_ip}): starting login flow")

    # Generate a PKCE pair per flow
    code_verifier, code_challenge = _pkce_pair()

    session = requests.Session()
    session.verify = VERIFY_SSL
    session.headers.update({"X-Forwarded-For": user_ip, "User-Agent": user_ua})

    # Internal Docker hostname (e.g. "keycloak:8443")
    _internal = KEYCLOAK_URL.split("://")[1]

    def _rewrite(url: str) -> str:
        """Replace external Keycloak hostname with the internal Docker hostname."""
        return (
            url
            .replace(f"{KEYCLOAK_HOSTNAME}:8443", _internal)
            .replace(KEYCLOAK_HOSTNAME, _internal)
        )

    try:
        # ── Step 1: Authorization request (PKCE) ─────────────────────────
        auth_params = {
            "client_id":             LOGIN_CLIENT_ID,
            "redirect_uri":          _redirect_uri(realm),
            "response_type":         "code",
            "scope":                 "openid",
            "code_challenge":        code_challenge,
            "code_challenge_method": "S256",
            "nonce":                 base64.urlsafe_b64encode(os.urandom(8)).decode("ascii"),
        }

        resp = session.get(_auth_url(realm), params=auth_params,
                           allow_redirects=False, timeout=15)

        # Follow redirects manually so we can rewrite hostnames at each hop
        hops = 10
        while resp.status_code in (301, 302, 303, 307, 308) and hops > 0:
            location = _rewrite(resp.headers.get("Location", ""))
            resp = session.get(location, allow_redirects=False, timeout=15)
            hops -= 1

        if resp.status_code != 200:
            log.warning(
                f"  [{realm}] {username}: auth page failed "
                f"(HTTP {resp.status_code}) body={resp.text[:300]}"
            )
            _inc("user_login_failures")
            return

        # ── Extract Keycloak login form action URL ────────────────────────
        # Keycloak renders: <form id="kc-form-login" ... action="...">
        action_match = re.search(
            r'<form\b[^>]*\bid=["\']kc-form-login["\'][^>]*\baction=["\']([^"\']+)["\']',
            resp.text, re.IGNORECASE | re.DOTALL,
        )
        if not action_match:
            # Fallback: look for action= inside any form tag
            action_match = re.search(
                r'<form\b[^>]*\baction=["\']([^"\']+)["\']',
                resp.text, re.IGNORECASE | re.DOTALL,
            )

        if not action_match:
            log.warning(
                f"  [{realm}] {username}: could not find login form action — "
                f"page snippet={resp.text[:400]}"
            )
            _inc("user_login_failures")
            return

        login_action = _rewrite(action_match.group(1).replace("&amp;", "&"))
        log.debug(f"  [{realm}] {username}: login action → {login_action[:120]}")
        _delay()

        # ── Step 2: Submit login form ─────────────────────────────────────
        resp = session.post(
            login_action,
            data={"username": username, "password": password},
            allow_redirects=False,
            timeout=15,
        )

        if resp.status_code not in (302, 303):
            log.warning(
                f"  [{realm}] {username}: login POST failed "
                f"(HTTP {resp.status_code}) — wrong credentials or locked account. "
                f"body={resp.text[:300]}"
            )
            _inc("user_login_failures")
            return

        location = resp.headers.get("Location", "")
        code = parse_qs(urlparse(location).query).get("code", [None])[0]

        if not code:
            log.warning(
                f"  [{realm}] {username}: no authorization code in redirect "
                f"(Location: {location[:200]})"
            )
            _inc("user_login_failures")
            return

        _inc("user_logins")
        log.info(f"  [{realm}] {username}: ✅ login OK — got authorization code")
        _delay()

        # ── Step 3: Exchange code → tokens  (PKCE code_verifier) ─────────
        resp = session.post(
            _token_url(realm),
            data={
                "grant_type":    "authorization_code",
                "client_id":     LOGIN_CLIENT_ID,
                "code":          code,
                "redirect_uri":  _redirect_uri(realm),
                "code_verifier": code_verifier,   # ← PKCE requirement
            },
            timeout=15,
        )

        if resp.status_code != 200:
            log.warning(
                f"  [{realm}] {username}: code exchange failed "
                f"(HTTP {resp.status_code}) body={resp.text[:300]}"
            )
            _inc("code_exchange_failures")
            return

        tokens        = resp.json()
        access_token  = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")

        if not access_token:
            log.warning(f"  [{realm}] {username}: no access_token in exchange response")
            _inc("code_exchange_failures")
            return

        _inc("code_exchanges")
        log.info(f"  [{realm}] {username}: ✅ code exchange OK")
        _delay()

        # ── Step 4: Refresh token ─────────────────────────────────────────
        if refresh_token:
            resp = session.post(
                _token_url(realm),
                data={
                    "grant_type":    "refresh_token",
                    "client_id":     LOGIN_CLIENT_ID,
                    "refresh_token": refresh_token,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                _inc("user_refreshes")
                refresh_token = resp.json().get("refresh_token", refresh_token)
                log.info(f"  [{realm}] {username}: ✅ refresh OK")
            else:
                _inc("user_refresh_failures")
                log.warning(
                    f"  [{realm}] {username}: refresh failed "
                    f"(HTTP {resp.status_code}) body={resp.text[:200]}"
                )
            _delay()
        else:
            log.debug(f"  [{realm}] {username}: no refresh_token — skipping refresh step")

        # ── Step 5: Logout ────────────────────────────────────────────────
        resp = session.post(
            _logout_url(realm),
            data={"client_id": LOGIN_CLIENT_ID, "refresh_token": refresh_token},
            timeout=15,
        )
        if resp.status_code in (200, 204):
            _inc("user_logouts")
            log.info(f"  [{realm}] {username}: ✅ logout OK")
        else:
            _inc("user_logout_failures")
            log.warning(
                f"  [{realm}] {username}: logout failed "
                f"(HTTP {resp.status_code}) body={resp.text[:200]}"
            )

    except requests.RequestException as exc:
        log.warning(f"  [{realm}] {username}: request error — {exc}")
        _inc("user_login_failures")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Client Flow: Client Credentials Grant
# ---------------------------------------------------------------------------


def client_credentials_flow(realm: str, realm_idx: int, local_idx: int) -> None:
    """
    Client credentials flow:
      1. POST /token  (grant_type=client_credentials)
      2. POST /token  (grant_type=refresh_token — only if server issues one)
    """
    if shutdown_requested:
        return

    client_id     = _client_id(local_idx)
    client_secret = _client_secret(local_idx)
    slot          = USERS_PER_REALM + local_idx    # offset slots past users
    client_ip     = _ip(realm_idx, slot)
    client_ua     = _ua(realm_idx, slot)
    headers       = {"X-Forwarded-For": client_ip, "User-Agent": client_ua}

    log.info(f"  [{realm}] {client_id} (ip={client_ip}): starting client_credentials flow")

    try:
        # ── Step 1: Client credentials token ─────────────────────────────
        resp = requests.post(
            _token_url(realm),
            data={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
            },
            headers=headers,
            verify=VERIFY_SSL,
            timeout=15,
        )

        if resp.status_code != 200:
            log.warning(
                f"  [{realm}] {client_id}: client_credentials failed "
                f"(HTTP {resp.status_code}) body={resp.text[:300]}"
            )
            _inc("client_token_failures")
            return

        tokens        = resp.json()
        refresh_token = tokens.get("refresh_token")

        _inc("client_tokens")
        log.info(f"  [{realm}] {client_id}: ✅ client_credentials OK")
        _delay()

        # ── Step 2: Refresh token (optional — not always issued) ──────────
        if refresh_token:
            resp = requests.post(
                _token_url(realm),
                data={
                    "grant_type":    "refresh_token",
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                },
                headers=headers,
                verify=VERIFY_SSL,
                timeout=15,
            )
            if resp.status_code == 200:
                _inc("client_refreshes")
                log.info(f"  [{realm}] {client_id}: ✅ client refresh OK")
            else:
                _inc("client_refresh_failures")
                log.warning(
                    f"  [{realm}] {client_id}: client refresh failed "
                    f"(HTTP {resp.status_code}) body={resp.text[:200]}"
                )
        else:
            log.debug(
                f"  [{realm}] {client_id}: no refresh_token in response "
                "(enable 'Use refresh tokens for client credentials grant' in client settings)"
            )

    except requests.RequestException as exc:
        log.warning(f"  [{realm}] {client_id}: request error — {exc}")
        _inc("client_token_failures")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_realm(realm: str, realm_idx: int) -> None:
    """Run all user flows then all client flows for one realm."""
    if shutdown_requested:
        return

    # ── User flows ────────────────────────────────────────────────────────
    log.info(
        f"[{realm}] ▶ user flows: {USERS_PER_REALM} users"
        f"{' (global index)' if USER_INDEX_GLOBAL else ' (per-realm index)'}"
        f", {CONCURRENCY} threads"
    )
    with ThreadPoolExecutor(
        max_workers=CONCURRENCY, thread_name_prefix=f"{realm}-user"
    ) as pool:
        futures = {
            pool.submit(user_login_flow, realm, realm_idx, i): i
            for i in range(USERS_PER_REALM)
            if not shutdown_requested
        }
        for fut in as_completed(futures):
            if shutdown_requested:
                break
            try:
                fut.result()
            except Exception as exc:
                log.error(f"[{realm}] user flow error (idx={futures[fut]}): {exc}")

    if shutdown_requested:
        return

    # ── Client flows ──────────────────────────────────────────────────────
    log.info(
        f"[{realm}] ▶ client flows: {CLIENTS_PER_REALM} clients, {CONCURRENCY} threads"
    )
    with ThreadPoolExecutor(
        max_workers=CONCURRENCY, thread_name_prefix=f"{realm}-client"
    ) as pool:
        futures = {
            pool.submit(client_credentials_flow, realm, realm_idx, i): i
            for i in range(CLIENTS_PER_REALM)
            if not shutdown_requested
        }
        for fut in as_completed(futures):
            if shutdown_requested:
                break
            try:
                fut.result()
            except Exception as exc:
                log.error(f"[{realm}] client flow error (idx={futures[fut]}): {exc}")


def run_all() -> None:
    """Run all realms sequentially."""
    log.info(f"Starting simulation across {REALM_COUNT} realm(s)...")
    for i in range(REALM_COUNT):
        if shutdown_requested:
            break
        run_realm(f"{REALM_PREFIX}{i}", i)
    _print_stats()


def _print_stats() -> None:
    with _stats_lock:
        s = dict(stats)
    total_ok = (
        s["user_logins"] + s["code_exchanges"] + s["user_refreshes"]
        + s["user_logouts"] + s["client_tokens"] + s["client_refreshes"]
    )
    total_fail = (
        s["user_login_failures"] + s["code_exchange_failures"]
        + s["user_refresh_failures"] + s["user_logout_failures"]
        + s["client_token_failures"] + s["client_refresh_failures"]
    )
    log.info("─" * 55)
    log.info("  Traffic Statistics")
    log.info("─" * 55)
    log.info(f"  User logins          : {s['user_logins']:>5} ok  /  {s['user_login_failures']:>5} failed")
    log.info(f"  Code exchanges       : {s['code_exchanges']:>5} ok  /  {s['code_exchange_failures']:>5} failed")
    log.info(f"  User refreshes       : {s['user_refreshes']:>5} ok  /  {s['user_refresh_failures']:>5} failed")
    log.info(f"  User logouts         : {s['user_logouts']:>5} ok  /  {s['user_logout_failures']:>5} failed")
    log.info(f"  Client tokens        : {s['client_tokens']:>5} ok  /  {s['client_token_failures']:>5} failed")
    log.info(f"  Client refreshes     : {s['client_refreshes']:>5} ok  /  {s['client_refresh_failures']:>5} failed")
    log.info("─" * 55)
    log.info(f"  TOTAL                : {total_ok:>5} ok  /  {total_fail:>5} failed")
    log.info("─" * 55)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("=" * 55)
    log.info("  Keycloak Traffic Simulator")
    log.info("=" * 55)
    log.info(f"  Keycloak URL        : {KEYCLOAK_URL}")
    log.info(f"  Base path           : {KEYCLOAK_BASE_PATH}")
    log.info(f"  Hostname            : {KEYCLOAK_HOSTNAME}")
    log.info(f"  Realm count         : {REALM_COUNT}")
    log.info(f"  Users per realm     : {USERS_PER_REALM}")
    log.info(f"  Clients per realm   : {CLIENTS_PER_REALM}")
    log.info(f"  Realm prefix        : {REALM_PREFIX}")
    log.info(f"  Login client        : {LOGIN_CLIENT_ID}")
    log.info(f"  User name pattern   : {USER_NAME_PATTERN}")
    log.info(f"  User pass pattern   : {USER_PASS_PATTERN}")
    log.info(f"  Client ID pattern   : {CLIENT_ID_PATTERN}")
    log.info(f"  Global user index   : {USER_INDEX_GLOBAL}")
    log.info(f"  Concurrency         : {CONCURRENCY}")
    log.info(f"  Loop                : {LOOP}")
    log.info(f"  Loop count          : {'infinite' if LOOP_COUNT == 0 else LOOP_COUNT}")
    log.info(f"  Loop delay          : {LOOP_DELAY}s")
    log.info(f"  Request delay       : {REQUEST_DELAY}s")
    log.info("=" * 55)

    wait_for_keycloak()
    wait_for_realms()

    if LOOP:
        iteration = 0
        while not shutdown_requested:
            iteration += 1
            if LOOP_COUNT > 0 and iteration > LOOP_COUNT:
                log.info(f"Completed {LOOP_COUNT} iteration(s) — stopping.")
                break
            log.info("")
            log.info(f"=== Iteration {iteration}{f'/{LOOP_COUNT}' if LOOP_COUNT > 0 else ''} ===")
            run_all()
            if not shutdown_requested and (LOOP_COUNT == 0 or iteration < LOOP_COUNT):
                log.info(f"Sleeping {LOOP_DELAY}s before next iteration...")
                for _ in range(LOOP_DELAY):
                    if shutdown_requested:
                        break
                    time.sleep(1)
    else:
        run_all()

    log.info("Traffic simulator stopped.")


if __name__ == "__main__":
    main()

