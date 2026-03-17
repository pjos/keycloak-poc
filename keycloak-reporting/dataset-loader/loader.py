#!/usr/bin/env python3
"""
Keycloak Dataset Loader

Provisions realms, clients, and users in Keycloak using the dataset provider API.
After provisioning, enables the Kafka event listener on every created realm so
that authentication events are forwarded to Kafka automatically.
On shutdown (SIGTERM/SIGINT), automatically removes all created realms.

Environment variables:
  KEYCLOAK_URL            - Keycloak base URL          (default: https://keycloak:8443)
  KEYCLOAK_BASE_PATH      - Keycloak base path         (default: /auth)
  KEYCLOAK_USER           - Admin username             (default: admin)
  KEYCLOAK_PASSWORD       - Admin password             (default: admin)
  REALM_COUNT             - Number of realms to create (default: 2)
  CLIENTS_PER_REALM       - Number of clients per realm (default: 5)
  USERS_PER_REALM         - Number of users per realm  (default: 10)
  REALM_PREFIX            - Realm name prefix          (default: realm-)
  VERIFY_SSL              - Verify SSL certificates    (default: false)
  KAFKA_EVENT_LISTENER    - Keycloak event listener ID for the Kafka plugin
                            (default: event-listener-kafka)
"""

import os
import sys
import signal
import time
import logging
import urllib3
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "https://keycloak:8443")
KEYCLOAK_BASE_PATH = os.getenv("KEYCLOAK_BASE_PATH", "/auth")
KEYCLOAK_USER = os.getenv("KEYCLOAK_USER", "admin")
KEYCLOAK_PASSWORD = os.getenv("KEYCLOAK_PASSWORD", "admin")
REALM_COUNT = int(os.getenv("REALM_COUNT", "2"))
CLIENTS_PER_REALM = int(os.getenv("CLIENTS_PER_REALM", "5"))
USERS_PER_REALM = int(os.getenv("USERS_PER_REALM", "10"))
REALM_PREFIX = os.getenv("REALM_PREFIX", "realm-")
VERIFY_SSL = os.getenv("VERIFY_SSL", "false").lower() in ("true", "1", "yes")

# ID of the Keycloak SPI event listener registered by keycloak-kafka.jar
KAFKA_EVENT_LISTENER = os.getenv("KAFKA_EVENT_LISTENER", "event-listener-kafka")

BASE_URL = f"{KEYCLOAK_URL}{KEYCLOAK_BASE_PATH}"
ADMIN_URL = f"{KEYCLOAK_URL}{KEYCLOAK_BASE_PATH}/admin/realms"
DATASET_URL = f"{BASE_URL}/realms/master/dataset"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dataset-loader")

if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

shutdown_requested = False
realms_created = False


def request_shutdown(signum, frame):
    """Signal handler for graceful shutdown."""
    global shutdown_requested
    sig_name = signal.Signals(signum).name
    log.info(f"Received {sig_name} — starting cleanup...")
    shutdown_requested = True


signal.signal(signal.SIGTERM, request_shutdown)
signal.signal(signal.SIGINT, request_shutdown)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def wait_for_keycloak(timeout: int = 300):
    """Wait until Keycloak is reachable."""
    log.info(f"Waiting for Keycloak at {KEYCLOAK_URL} ...")
    start = time.time()
    while time.time() - start < timeout:
        if shutdown_requested:
            sys.exit(0)
        try:
            r = requests.get(
                f"{BASE_URL}/realms/master/.well-known/openid-configuration",
                verify=VERIFY_SSL,
                timeout=5,
            )
            if r.status_code == 200:
                log.info("Keycloak is ready.")
                return
        except requests.ConnectionError:
            pass
        time.sleep(3)
    log.error(f"Keycloak not reachable after {timeout}s — aborting.")
    sys.exit(1)


def call_dataset_api(action: str, params: dict) -> bool:
    """Call the Keycloak dataset provider API."""
    url = f"{DATASET_URL}/{action}"
    log.info(f"  → {action} {params}")
    try:
        r = requests.get(url, params=params, verify=VERIFY_SSL, timeout=300)
        if r.status_code == 200:
            log.info(f"    ✅ {action} succeeded")
            return True
        else:
            log.error(f"    ❌ {action} failed (HTTP {r.status_code}): {r.text[:200]}")
            return False
    except requests.RequestException as e:
        log.error(f"    ❌ {action} failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Admin API helpers
# ---------------------------------------------------------------------------


def get_admin_token() -> str | None:
    """
    Obtain a short-lived admin Bearer token from the master realm.
    Uses the 'admin-cli' client with Resource Owner Password Credentials.
    """
    url = f"{BASE_URL}/realms/master/protocol/openid-connect/token"
    try:
        r = requests.post(
            url,
            data={
                "grant_type": "password",
                "client_id":  "admin-cli",
                "username":   KEYCLOAK_USER,
                "password":   KEYCLOAK_PASSWORD,
            },
            verify=VERIFY_SSL,
            timeout=30,
        )
        if r.status_code == 200:
            token = r.json().get("access_token")
            log.debug("Admin token obtained successfully.")
            return token
        log.error(f"  ❌ Failed to get admin token (HTTP {r.status_code}): {r.text[:200]}")
    except requests.RequestException as e:
        log.error(f"  ❌ Failed to get admin token: {e}")
    return None


def configure_realm_kafka_listener(realm: str, token: str) -> bool:
    """
    Enable the Kafka event listener and user/admin event recording for a realm.

    Steps:
      1. GET  /admin/realms/{realm}  → read current eventsListeners
      2. Append KAFKA_EVENT_LISTENER if not already present
      3. PUT  /admin/realms/{realm}  → persist updated config
    """
    url = f"{ADMIN_URL}/{realm}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }

    # 1. Read current realm configuration
    try:
        r = requests.get(url, headers=headers, verify=VERIFY_SSL, timeout=30)
    except requests.RequestException as e:
        log.error(f"  ❌ [{realm}] Cannot read realm config: {e}")
        return False

    if r.status_code != 200:
        log.error(f"  ❌ [{realm}] GET realm failed (HTTP {r.status_code}): {r.text[:200]}")
        return False

    realm_data = r.json()
    listeners  = realm_data.get("eventsListeners", ["jboss-logging"])

    # 2. Add Kafka listener if missing
    if KAFKA_EVENT_LISTENER in listeners:
        log.info(f"  ℹ️  [{realm}] Kafka listener already configured — skipping")
        return True

    listeners.append(KAFKA_EVENT_LISTENER)

    # 3. Update realm: activate listener + turn on event recording
    payload = {
        "eventsListeners":         listeners,
        "eventsEnabled":           True,   # record user events (LOGIN, LOGOUT, …)
        "adminEventsEnabled":      True,   # record admin events
        "adminEventsDetailsEnabled": True, # include full request representation
    }

    try:
        r = requests.put(url, json=payload, headers=headers, verify=VERIFY_SSL, timeout=30)
    except requests.RequestException as e:
        log.error(f"  ❌ [{realm}] Cannot update realm config: {e}")
        return False

    if r.status_code in (200, 204):
        log.info(f"  ✅ [{realm}] Kafka event listener enabled (listeners={listeners})")
        return True

    log.error(f"  ❌ [{realm}] PUT realm failed (HTTP {r.status_code}): {r.text[:200]}")
    return False


def wait_for_realm(realm: str, timeout: int = 180) -> bool:
    """
    Wait until a realm is fully created and accessible via its openid-configuration
    endpoint (no authentication required).
    Returns True when the realm is ready, False on timeout.
    """
    url = f"{BASE_URL}/realms/{realm}/.well-known/openid-configuration"
    deadline = time.time() + timeout
    log.info(f"  ⏳ [{realm}] Waiting for realm to become available...")
    while time.time() < deadline:
        if shutdown_requested:
            return False
        try:
            r = requests.get(url, verify=VERIFY_SSL, timeout=5)
            if r.status_code == 200:
                log.info(f"  ✅ [{realm}] Realm is ready")
                return True
        except requests.ConnectionError:
            pass
        time.sleep(3)
    log.error(f"  ❌ [{realm}] Realm not available after {timeout}s — skipping Kafka configuration")
    return False


def configure_kafka_listeners() -> None:
    """
    Enable the Kafka event listener on every realm created by the dataset loader.
    Waits for each realm to be accessible before attempting configuration.
    Re-authenticates if the admin token has expired (one retry per realm).
    """
    log.info(f"Configuring Kafka event listener '{KAFKA_EVENT_LISTENER}' on {REALM_COUNT} realm(s)...")

    token = get_admin_token()
    if not token:
        log.error("Cannot configure Kafka listeners: failed to obtain admin token.")
        return

    for i in range(REALM_COUNT):
        if shutdown_requested:
            break

        realm = f"{REALM_PREFIX}{i}"

        # Wait for the realm to be fully created before calling the Admin API
        if not wait_for_realm(realm):
            continue

        success = configure_realm_kafka_listener(realm, token)

        # If it failed (token may have expired), refresh the token and retry once
        if not success:
            log.info(f"  ↻  [{realm}] Refreshing admin token and retrying…")
            token = get_admin_token()
            if token:
                configure_realm_kafka_listener(realm, token)

    log.info("Kafka listener configuration complete.")


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def create_realms():
    """Create realms with clients and users."""
    log.info(f"Creating {REALM_COUNT} realm(s) with {CLIENTS_PER_REALM} client(s) and {USERS_PER_REALM} user(s) each...")
    return call_dataset_api("create-realms", {
        "count": REALM_COUNT,
        "clients-per-realm": CLIENTS_PER_REALM,
        "users-per-realm": USERS_PER_REALM,
    })


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def remove_realms():
    """Remove all realms created by the dataset provider."""
    log.info("Removing all dataset realms...")
    call_dataset_api("remove-realms", {"remove-all": "true"})
    log.info("Cleanup complete.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    global realms_created

    log.info("========================================")
    log.info("  Keycloak Dataset Loader")
    log.info("========================================")
    log.info(f"  Keycloak URL       : {KEYCLOAK_URL}")
    log.info(f"  Realm count        : {REALM_COUNT}")
    log.info(f"  Clients per realm  : {CLIENTS_PER_REALM}")
    log.info(f"  Users per realm    : {USERS_PER_REALM}")
    log.info(f"  Realm prefix       : {REALM_PREFIX}")
    log.info(f"  Kafka listener     : {KAFKA_EVENT_LISTENER}")
    log.info("========================================")

    # 1. Wait for Keycloak
    wait_for_keycloak()

    if shutdown_requested:
        return

    # 2. Create realms (with clients and users)
    if create_realms():
        realms_created = True

    if shutdown_requested:
        remove_realms()
        return

    # 3. Enable Kafka event listener on every created realm
    configure_kafka_listeners()

    if shutdown_requested:
        remove_realms()
        return

    log.info("")
    log.info("========================================")
    log.info("  Provisioning complete!")
    log.info(f"  {REALM_COUNT} realm(s)")
    log.info(f"  {CLIENTS_PER_REALM} client(s) per realm")
    log.info(f"  {USERS_PER_REALM} user(s) per realm")
    log.info(f"  Kafka listener '{KAFKA_EVENT_LISTENER}' enabled on all realms")
    log.info("========================================")
    log.info("")
    log.info("Waiting for shutdown signal (SIGTERM/SIGINT)...")
    log.info("On shutdown, all created realms will be removed.")

    # 4. Wait for shutdown signal
    while not shutdown_requested:
        time.sleep(1)

    # 5. Cleanup on shutdown
    remove_realms()


if __name__ == "__main__":
    main()

