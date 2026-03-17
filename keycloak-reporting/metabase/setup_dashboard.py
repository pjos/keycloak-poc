#!/usr/bin/env python3
"""
Metabase Dashboard Setup

Configures Metabase with:
  1. ClickHouse database connection
  2. A comprehensive Keycloak Events dashboard with:
     - Dashboard filters: date range + realm
     - KPI scalars
     - Time series evolution charts for every metric
     - Full event type coverage (LOGIN, LOGIN_ERROR, CODE_TO_TOKEN,
       CLIENT_LOGIN, REFRESH_TOKEN, LOGOUT, REGISTER, etc.)
     - Admin events coverage
     - Varied chart types (scalar, line, bar, area, pie, row, table)

Environment variables:
  METABASE_URL         - Metabase base URL (default: http://metabase:3000)
  METABASE_EMAIL       - Admin email (default: admin@keycloak.local)
  METABASE_PASSWORD    - Admin password (default: Admin123!)
  METABASE_FIRST_NAME  - Admin first name (default: Admin)
  METABASE_LAST_NAME   - Admin last name (default: Keycloak)
  CLICKHOUSE_HOST      - ClickHouse hostname (default: clickhouse)
  CLICKHOUSE_PORT      - ClickHouse port (default: 8123)
  CLICKHOUSE_DB        - ClickHouse database (default: keycloak)
  CLICKHOUSE_USER      - ClickHouse user (default: default)
  CLICKHOUSE_PASSWORD  - ClickHouse password (default: clickhouse)
"""

import os
import sys
import time
import logging
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

METABASE_URL = os.getenv("METABASE_URL", "http://metabase:3000")
METABASE_EMAIL = os.getenv("METABASE_EMAIL", "admin@keycloak.local")
METABASE_PASSWORD = os.getenv("METABASE_PASSWORD", "Admin123!")
METABASE_FIRST_NAME = os.getenv("METABASE_FIRST_NAME", "Admin")
METABASE_LAST_NAME = os.getenv("METABASE_LAST_NAME", "Keycloak")
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_DB = os.getenv("CLICKHOUSE_DB", "keycloak")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "clickhouse")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("metabase-setup")

# ---------------------------------------------------------------------------
# Template tags and filter clause
# ---------------------------------------------------------------------------
# Every native query uses {{date_from}}, {{date_to}}, {{realm}} placeholders.
# These are wired to dashboard-level filter parameters.

TEMPLATE_TAGS = {
    "date_from": {
        "id": "date_from",
        "name": "date_from",
        "display-name": "Date From",
        "type": "text",
        "default": "2020-01-01",
    },
    "date_to": {
        "id": "date_to",
        "name": "date_to",
        "display-name": "Date To",
        "type": "text",
        "default": "2099-12-31",
    },
    "realm": {
        "id": "realm",
        "name": "realm",
        "display-name": "Realm",
        "type": "text",
        "required": False,
    },
}

# SQL fragment injected into every WHERE clause
# realm uses Metabase optional clause syntax [[ ... ]]
# When realm is not provided, the [[ ]] block is removed entirely
F = """
  event_time >= parseDateTimeBestEffort({{ date_from }})
  AND event_time <= parseDateTimeBestEffort({{ date_to }})
  [[ AND realmId = {{ realm }} ]]
"""

# Same for admin_events table
FA = """
  event_time >= parseDateTimeBestEffort({{ date_from }})
  AND event_time <= parseDateTimeBestEffort({{ date_to }})
  [[ AND realmId = {{ realm }} ]]
"""

# ---------------------------------------------------------------------------
# Metabase API client
# ---------------------------------------------------------------------------


class MetabaseClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def wait_for_ready(self, timeout=300):
        log.info(f"Waiting for Metabase at {self.base_url} ...")
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = self.session.get(f"{self.base_url}/api/health", timeout=5)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    log.info("Metabase is ready.")
                    return True
            except Exception:
                pass
            time.sleep(5)
        log.error("Metabase not ready in time.")
        return False

    def setup(self):
        r = self.session.get(f"{self.base_url}/api/session/properties")
        props = r.json()
        if props.get("setup-token"):
            setup_token = props["setup-token"]
            log.info("Running first-time Metabase setup...")
            payload = {
                "token": setup_token,
                "user": {
                    "email": METABASE_EMAIL,
                    "password": METABASE_PASSWORD,
                    "first_name": METABASE_FIRST_NAME,
                    "last_name": METABASE_LAST_NAME,
                    "site_name": "Keycloak Reporting",
                },
                "prefs": {
                    "site_name": "Keycloak Reporting",
                    "site_locale": "en",
                    "allow_tracking": False,
                },
            }
            r = self.session.post(f"{self.base_url}/api/setup", json=payload)
            if r.status_code == 200:
                self.session.headers["X-Metabase-Session"] = r.json().get("id")
                log.info("Metabase setup complete.")
            else:
                log.error(f"Setup failed: {r.status_code} {r.text[:200]}")
                sys.exit(1)
        else:
            log.info("Metabase already set up, logging in...")
            self.login()

    def login(self):
        r = self.session.post(
            f"{self.base_url}/api/session",
            json={"username": METABASE_EMAIL, "password": METABASE_PASSWORD},
        )
        if r.status_code == 200:
            self.session.headers["X-Metabase-Session"] = r.json().get("id")
            log.info("Logged in.")
        else:
            log.error(f"Login failed: {r.status_code} {r.text[:200]}")
            sys.exit(1)

    def add_clickhouse_db(self) -> int:
        r = self.session.get(f"{self.base_url}/api/database")
        for db in r.json().get("data", []):
            if db.get("engine") == "clickhouse":
                log.info(f"ClickHouse DB already exists (id={db['id']})")
                return db["id"]
        payload = {
            "engine": "clickhouse",
            "name": "ClickHouse - Keycloak Events",
            "details": {
                "host": CLICKHOUSE_HOST,
                "port": CLICKHOUSE_PORT,
                "dbname": CLICKHOUSE_DB,
                "user": CLICKHOUSE_USER,
                "password": CLICKHOUSE_PASSWORD,
                "ssl": False,
            },
            "is_full_sync": True,
            "auto_run_queries": True,
        }
        r = self.session.post(f"{self.base_url}/api/database", json=payload)
        if r.status_code == 200:
            db_id = r.json()["id"]
            log.info(f"ClickHouse DB added (id={db_id})")
            self.session.post(f"{self.base_url}/api/database/{db_id}/sync_schema")
            time.sleep(10)
            return db_id
        else:
            log.error(f"Failed to add DB: {r.status_code} {r.text[:200]}")
            sys.exit(1)

    def card(self, name, query, db_id, display="table", viz=None):
        """Create a saved question with template tags."""
        payload = {
            "name": name,
            "dataset_query": {
                "type": "native",
                "native": {"query": query, "template-tags": TEMPLATE_TAGS},
                "database": db_id,
            },
            "display": display,
            "visualization_settings": viz or {},
        }
        r = self.session.post(f"{self.base_url}/api/card", json=payload)
        if r.status_code == 200:
            cid = r.json()["id"]
            log.info(f"  ✅ {name} (id={cid}, {display})")
            return cid
        else:
            log.error(f"  ❌ {name}: {r.status_code} {r.text[:200]}")
            return -1

    def create_dashboard(self, name, description=""):
        r = self.session.post(f"{self.base_url}/api/dashboard",
                              json={"name": name, "description": description})
        if r.status_code == 200:
            did = r.json()["id"]
            log.info(f"Dashboard '{name}' (id={did})")
            return did
        log.error(f"Dashboard failed: {r.status_code} {r.text[:200]}")
        return -1

    def wire_dashboard(self, dashboard_id, cards):
        """Add cards + dashboard filter parameters + parameter mappings."""
        dashcards = []
        for idx, c in enumerate(cards):
            dashcards.append({
                "id": -(idx + 1),
                "card_id": c["id"],
                "row": c.get("row", 0),
                "col": c.get("col", 0),
                "size_x": c.get("sx", 10),
                "size_y": c.get("sy", 5),
                "parameter_mappings": [
                    {"parameter_id": "date_from", "card_id": c["id"],
                     "target": ["variable", ["template-tag", "date_from"]]},
                    {"parameter_id": "date_to", "card_id": c["id"],
                     "target": ["variable", ["template-tag", "date_to"]]},
                    {"parameter_id": "realm", "card_id": c["id"],
                     "target": ["variable", ["template-tag", "realm"]]},
                ],
            })

        parameters = [
            {"id": "date_from", "name": "Date From", "slug": "date_from",
             "type": "string/=", "sectionId": "string", "default": "2020-01-01"},
            {"id": "date_to", "name": "Date To", "slug": "date_to",
             "type": "string/=", "sectionId": "string", "default": "2099-12-31"},
            {"id": "realm", "name": "Realm", "slug": "realm",
             "type": "string/=", "sectionId": "string"},
        ]

        r = self.session.put(
            f"{self.base_url}/api/dashboard/{dashboard_id}",
            json={"dashcards": dashcards, "parameters": parameters},
        )
        if r.status_code == 200:
            log.info(f"  Wired {len(cards)} cards with 3 filters")
        else:
            log.error(f"  Wire failed: {r.status_code} {r.text[:300]}")


# ---------------------------------------------------------------------------
# Dashboard builder
# ---------------------------------------------------------------------------

H = 10  # half-width
W = 20  # full-width


def build_dashboard(mc: MetabaseClient, db: int):
    log.info("Creating cards...")
    cards = []
    row = 0

    def add(name, sql, display, col, r, sx, sy, viz=None):
        cid = mc.card(name, sql, db, display, viz)
        if cid > 0:
            cards.append({"id": cid, "col": col, "row": r, "sx": sx, "sy": sy})

    # ======================================================================
    # ROW 0 — KPI scalars  (5 × 4-col cards)
    # ======================================================================
    add("🟢 Active Users", f"""
        SELECT uniqExact(userId) AS v
        FROM keycloak.keycloak_events WHERE type='LOGIN' AND userId!='' AND {F}
    """, "scalar", 0, row, 4, 3)

    add("🟢 Active Clients", f"""
        SELECT uniqExact(clientId) AS v
        FROM keycloak.keycloak_events
        WHERE type IN ('LOGIN','CLIENT_LOGIN','CODE_TO_TOKEN') AND clientId!='' AND {F}
    """, "scalar", 4, row, 4, 3)

    add("🔵 Total Sessions", f"""
        SELECT uniqExact(sessionId) AS v
        FROM keycloak.keycloak_events WHERE sessionId!='' AND {F}
    """, "scalar", 8, row, 4, 3)

    add("🔴 Login Errors", f"""
        SELECT count() AS v
        FROM keycloak.keycloak_events WHERE type='LOGIN_ERROR' AND {F}
    """, "scalar", 12, row, 4, 3)

    add("🟡 Total Logouts", f"""
        SELECT uniqExact(userId) AS v
        FROM keycloak.keycloak_events WHERE type='LOGOUT' AND userId!='' AND {F}
    """, "scalar", 16, row, 4, 3)
    row += 3

    # ======================================================================
    # ROW 1 — Global event overview (stacked area + pie breakdown)
    # ======================================================================
    add("📈 All Events over Time", f"""
        SELECT toStartOfMinute(event_time) AS t, type, count() AS n
        FROM keycloak.keycloak_events WHERE {F}
        GROUP BY t, type ORDER BY t
    """, "area", 0, row, 12, 6, {
        "stackable.stack_type": "stacked",
        "graph.x_axis.title_text": "Time", "graph.y_axis.title_text": "Events"})

    add("🥧 Events Breakdown", f"""
        SELECT type, count() AS n
        FROM keycloak.keycloak_events WHERE {F}
        GROUP BY type ORDER BY n DESC
    """, "pie", 12, row, 8, 6, {"pie.show_total": True})
    row += 6

    # ======================================================================
    # ROW 2 — Logins over time + Login errors over time
    # ======================================================================
    add("📈 Logins over Time", f"""
        SELECT toStartOfMinute(event_time) AS t, count() AS n
        FROM keycloak.keycloak_events WHERE type='LOGIN' AND {F}
        GROUP BY t ORDER BY t
    """, "line", 0, row, H, 5, {
        "graph.x_axis.title_text": "Time", "graph.y_axis.title_text": "Logins"})

    add("📈 Login Errors over Time", f"""
        SELECT toStartOfMinute(event_time) AS t, count() AS n
        FROM keycloak.keycloak_events WHERE type='LOGIN_ERROR' AND {F}
        GROUP BY t ORDER BY t
    """, "line", H, row, H, 5, {
        "graph.x_axis.title_text": "Time", "graph.y_axis.title_text": "Errors",
        "graph.colors": ["#EF8C8C"]})
    row += 5

    # ======================================================================
    # ROW 3 — Logins per user + Login errors detail table
    # ======================================================================
    add("📊 Logins per User (Top 25)", f"""
        SELECT userId, count() AS logins
        FROM keycloak.keycloak_events
        WHERE type='LOGIN' AND userId!='' AND {F}
        GROUP BY userId ORDER BY logins DESC LIMIT 25
    """, "row", 0, row, H, 7)

    add("🔴 Login Error Details", f"""
        SELECT type, error, userId, clientId, ipAddress,
               count() AS errors, max(event_time) AS last
        FROM keycloak.keycloak_events
        WHERE type='LOGIN_ERROR' AND {F}
        GROUP BY type, error, userId, clientId, ipAddress
        ORDER BY errors DESC LIMIT 50
    """, "table", H, row, H, 7)
    row += 7

    # ======================================================================
    # ROW 4 — Sessions per user (bar) + sessions per client (pie)
    # ======================================================================
    add("📊 Sessions per User (Top 25)", f"""
        SELECT userId, uniqExact(sessionId) AS s
        FROM keycloak.keycloak_events
        WHERE sessionId!='' AND userId!='' AND {F}
        GROUP BY userId ORDER BY s DESC LIMIT 25
    """, "bar", 0, row, H, 6, {
        "graph.x_axis.title_text": "User", "graph.y_axis.title_text": "Sessions"})

    add("🥧 Sessions per Client", f"""
        SELECT clientId, uniqExact(sessionId) AS s
        FROM keycloak.keycloak_events
        WHERE sessionId!='' AND clientId!='' AND {F}
        GROUP BY clientId ORDER BY s DESC LIMIT 15
    """, "pie", H, row, H, 6)
    row += 6

    # ======================================================================
    # ROW 5 — CODE_TO_TOKEN: donut by grant_type + over time stacked
    # ======================================================================
    add("🍩 Tokens by Grant Type", f"""
        SELECT JSONExtractString(details,'grant_type') AS gt, count() AS n
        FROM keycloak.keycloak_events
        WHERE type='CODE_TO_TOKEN'
          AND JSONExtractString(details,'grant_type')!='' AND {F}
        GROUP BY gt ORDER BY n DESC
    """, "pie", 0, row, 8, 6, {"pie.show_total": True})

    add("📈 Tokens over Time (by Grant Type)", f"""
        SELECT toStartOfMinute(event_time) AS t,
               JSONExtractString(details,'grant_type') AS gt, count() AS n
        FROM keycloak.keycloak_events
        WHERE type='CODE_TO_TOKEN'
          AND JSONExtractString(details,'grant_type')!='' AND {F}
        GROUP BY t, gt ORDER BY t
    """, "area", 8, row, 12, 6, {
        "stackable.stack_type": "stacked",
        "graph.x_axis.title_text": "Time", "graph.y_axis.title_text": "Tokens"})
    row += 6

    # ======================================================================
    # ROW 6 — CLIENT_LOGIN (client_credentials): per client + over time
    # ======================================================================
    add("📊 Client Credentials per Client (Top 20)", f"""
        SELECT clientId, count() AS n
        FROM keycloak.keycloak_events
        WHERE type='CLIENT_LOGIN' AND clientId!='' AND {F}
        GROUP BY clientId ORDER BY n DESC LIMIT 20
    """, "bar", 0, row, H, 6, {
        "graph.x_axis.title_text": "Client", "graph.y_axis.title_text": "Tokens"})

    add("📈 Client Credentials over Time", f"""
        SELECT toStartOfMinute(event_time) AS t, count() AS n
        FROM keycloak.keycloak_events
        WHERE type='CLIENT_LOGIN' AND {F}
        GROUP BY t ORDER BY t
    """, "line", H, row, H, 6, {
        "graph.x_axis.title_text": "Time", "graph.y_axis.title_text": "Client Tokens"})
    row += 6

    # ======================================================================
    # ROW 7 — REFRESH_TOKEN: per client + over time
    # ======================================================================
    add("📊 Refresh Tokens per Client (Top 20)", f"""
        SELECT clientId, count() AS n
        FROM keycloak.keycloak_events
        WHERE type='REFRESH_TOKEN' AND clientId!='' AND {F}
        GROUP BY clientId ORDER BY n DESC LIMIT 20
    """, "bar", 0, row, H, 6, {
        "graph.x_axis.title_text": "Client", "graph.y_axis.title_text": "Refreshes"})

    add("📈 Refresh Tokens over Time", f"""
        SELECT toStartOfMinute(event_time) AS t, count() AS n
        FROM keycloak.keycloak_events
        WHERE type='REFRESH_TOKEN' AND {F}
        GROUP BY t ORDER BY t
    """, "line", H, row, H, 6, {
        "graph.x_axis.title_text": "Time", "graph.y_axis.title_text": "Refreshes"})
    row += 6

    # ======================================================================
    # ROW 8 — LOGOUT over time + REGISTER over time
    # ======================================================================
    add("📈 Logouts over Time", f"""
        SELECT toStartOfMinute(event_time) AS t, count() AS n
        FROM keycloak.keycloak_events WHERE type='LOGOUT' AND {F}
        GROUP BY t ORDER BY t
    """, "line", 0, row, H, 5, {
        "graph.x_axis.title_text": "Time", "graph.y_axis.title_text": "Logouts"})

    add("📈 Registrations over Time", f"""
        SELECT toStartOfMinute(event_time) AS t, count() AS n
        FROM keycloak.keycloak_events WHERE type='REGISTER' AND {F}
        GROUP BY t ORDER BY t
    """, "line", H, row, H, 5, {
        "graph.x_axis.title_text": "Time", "graph.y_axis.title_text": "Registrations",
        "graph.colors": ["#88BF4D"]})
    row += 5

    # ======================================================================
    # ROW 9 — Events per Realm (stacked bar) + Top IPs
    # ======================================================================
    add("📊 Events per Realm", f"""
        SELECT realmId, type, count() AS n
        FROM keycloak.keycloak_events WHERE {F}
        GROUP BY realmId, type ORDER BY realmId, n DESC
    """, "bar", 0, row, H, 6, {
        "stackable.stack_type": "stacked",
        "graph.x_axis.title_text": "Realm", "graph.y_axis.title_text": "Events"})

    add("🌍 Top IPs", f"""
        SELECT ipAddress, count() AS events, uniqExact(userId) AS users,
               uniqExact(type) AS event_types
        FROM keycloak.keycloak_events
        WHERE ipAddress!='' AND {F}
        GROUP BY ipAddress ORDER BY events DESC LIMIT 20
    """, "table", H, row, H, 6)
    row += 6

    # ======================================================================
    # ROW 10 — User Agents + All Error Types
    # ======================================================================
    add("📱 Top User Agents", f"""
        SELECT substring(userAgent,1,80) AS ua,
               count() AS events, uniqExact(userId) AS users
        FROM keycloak.keycloak_events
        WHERE userAgent!='' AND {F}
        GROUP BY ua ORDER BY events DESC LIMIT 15
    """, "row", 0, row, H, 6)

    add("🔴 All Errors by Type", f"""
        SELECT type, error, count() AS n, uniqExact(userId) AS users,
               max(event_time) AS last
        FROM keycloak.keycloak_events
        WHERE error!='' AND {F}
        GROUP BY type, error ORDER BY n DESC
    """, "table", H, row, H, 6)
    row += 6

    # ======================================================================
    # ROW 11 — Misc events: TOKEN_EXCHANGE, INTROSPECT_TOKEN, USER_INFO_REQUEST
    # ======================================================================
    add("📈 Token Exchange / Introspect / UserInfo over Time", f"""
        SELECT toStartOfMinute(event_time) AS t, type, count() AS n
        FROM keycloak.keycloak_events
        WHERE type IN ('TOKEN_EXCHANGE','INTROSPECT_TOKEN',
                        'USER_INFO_REQUEST','USER_INFO_REQUEST_ERROR',
                        'PERMISSION_TOKEN') AND {F}
        GROUP BY t, type ORDER BY t
    """, "line", 0, row, H, 5, {
        "graph.x_axis.title_text": "Time", "graph.y_axis.title_text": "Events"})

    add("📊 Misc Event Counts", f"""
        SELECT type, count() AS n
        FROM keycloak.keycloak_events
        WHERE type IN ('TOKEN_EXCHANGE','INTROSPECT_TOKEN',
                        'USER_INFO_REQUEST','USER_INFO_REQUEST_ERROR',
                        'PERMISSION_TOKEN','REVOKE_GRANT','UPDATE_PROFILE',
                        'VERIFY_EMAIL','SEND_VERIFY_EMAIL',
                        'RESET_PASSWORD','SEND_RESET_PASSWORD',
                        'UPDATE_PASSWORD','UPDATE_TOTP','REMOVE_TOTP',
                        'GRANT_CONSENT','REVOKE_CONSENT',
                        'CLIENT_REGISTER','CLIENT_UPDATE','CLIENT_DELETE',
                        'IMPERSONATE','CUSTOM_REQUIRED_ACTION') AND {F}
        GROUP BY type ORDER BY n DESC
    """, "bar", H, row, H, 5, {
        "graph.x_axis.title_text": "Event Type", "graph.y_axis.title_text": "Count"})
    row += 5

    # ======================================================================
    # ROW 12 — Admin Events: stacked area + breakdown table
    # ======================================================================
    add("📈 Admin Events over Time", f"""
        SELECT toStartOfMinute(event_time) AS t, operationType, count() AS n
        FROM keycloak.keycloak_admin_events WHERE {FA}
        GROUP BY t, operationType ORDER BY t
    """, "area", 0, row, H, 6, {
        "stackable.stack_type": "stacked",
        "graph.x_axis.title_text": "Time", "graph.y_axis.title_text": "Admin Events"})

    add("📋 Admin Events Breakdown", f"""
        SELECT operationType, resourceType, count() AS n,
               uniqExact(authUserId) AS users
        FROM keycloak.keycloak_admin_events WHERE {FA}
        GROUP BY operationType, resourceType ORDER BY n DESC LIMIT 30
    """, "table", H, row, H, 6)
    row += 6

    # ======================================================================
    # ROW 13 — Recent events raw table
    # ======================================================================
    add("📋 Recent Events (last 200)", f"""
        SELECT event_time, type, realmId, clientId, userId,
               sessionId, ipAddress, error,
               substring(details,1,100) AS details
        FROM keycloak.keycloak_events WHERE {F}
        ORDER BY event_time DESC LIMIT 200
    """, "table", 0, row, W, 8)
    row += 8

    # ======================================================================
    # ROW 14 — Recent admin events raw table
    # ======================================================================
    add("📋 Recent Admin Events (last 100)", f"""
        SELECT event_time, operationType, resourceType, resourcePath,
               authClientId, authUserId, authIpAddress, error,
               substring(representation,1,100) AS repr
        FROM keycloak.keycloak_admin_events WHERE {FA}
        ORDER BY event_time DESC LIMIT 100
    """, "table", 0, row, W, 7)

    # ======================================================================
    # Create dashboard + wire filters
    # ======================================================================
    cards = [c for c in cards if c["id"] > 0]

    did = mc.create_dashboard(
        "🔐 Keycloak Events Dashboard",
        "Comprehensive Keycloak monitoring: logins, errors, sessions, tokens, "
        "refresh, logouts, registrations, client credentials, admin events. "
        "Filter by date range and realm.",
    )

    if did > 0 and cards:
        mc.wire_dashboard(did, cards)

    return did


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("========================================")
    log.info("  Metabase Dashboard Setup")
    log.info("========================================")

    mc = MetabaseClient(METABASE_URL)
    if not mc.wait_for_ready():
        sys.exit(1)

    mc.setup()
    db = mc.add_clickhouse_db()
    did = build_dashboard(mc, db)

    log.info("")
    log.info("========================================")
    log.info("  Setup complete!")
    log.info(f"  Dashboard : {METABASE_URL}/dashboard/{did}")
    log.info(f"  Login     : {METABASE_EMAIL} / {METABASE_PASSWORD}")
    log.info("========================================")


if __name__ == "__main__":
    main()

