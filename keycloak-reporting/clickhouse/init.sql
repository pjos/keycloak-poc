-- ==========================================================================
-- ClickHouse initialization script
-- Creates Kafka engine tables, MergeTree storage tables, and materialized
-- views to automatically ingest Kafka messages into ClickHouse.
-- ==========================================================================

CREATE DATABASE IF NOT EXISTS keycloak;

-- ==========================================================================
-- 1. Keycloak Events
-- ==========================================================================

-- Kafka engine table (acts as a consumer)
CREATE TABLE IF NOT EXISTS keycloak.keycloak_events_kafka (
    type           String,
    realmId        String,
    realmName      String,
    clientId       String,
    userId         String,
    sessionId      String,
    ipAddress      String,
    userAgent      String,
    `time`         UInt64,
    error          String,
    details        String
) ENGINE = Kafka()
SETTINGS
    kafka_broker_list = 'kafka:29092',
    kafka_topic_list = 'keycloak-events',
    kafka_group_name = 'clickhouse-keycloak-events',
    kafka_format = 'JSONEachRow',
    kafka_num_consumers = 1,
    kafka_handle_error_mode = 'stream';

-- MergeTree storage table (persistent, queryable)
CREATE TABLE IF NOT EXISTS keycloak.keycloak_events (
    type           String,
    realmId        String,
    realmName      String,
    clientId       String,
    userId         String,
    sessionId      String,
    ipAddress      String,
    userAgent      String,
    event_time     DateTime64(3) DEFAULT now64(3),
    error          String,
    details        String,
    inserted_at    DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (realmId, type, event_time)
PARTITION BY toYYYYMM(event_time);

-- Materialized view: pipes data from Kafka engine → MergeTree
CREATE MATERIALIZED VIEW IF NOT EXISTS keycloak.keycloak_events_mv
TO keycloak.keycloak_events AS
SELECT
    type,
    realmId,
    realmName,
    clientId,
    userId,
    sessionId,
    ipAddress,
    userAgent,
    fromUnixTimestamp64Milli(`time`) AS event_time,
    error,
    details
FROM keycloak.keycloak_events_kafka;

-- ==========================================================================
-- 2. Keycloak Admin Events
-- ==========================================================================

-- Kafka engine table
CREATE TABLE IF NOT EXISTS keycloak.keycloak_admin_events_kafka (
    operationType    String,
    realmId          String,
    resourceType     String,
    resourcePath     String,
    authRealmId      String,
    authClientId     String,
    authUserId       String,
    authIpAddress    String,
    `time`           UInt64,
    error            String,
    representation   String
) ENGINE = Kafka()
SETTINGS
    kafka_broker_list = 'kafka:29092',
    kafka_topic_list = 'keycloak-admin-events',
    kafka_group_name = 'clickhouse-keycloak-admin-events',
    kafka_format = 'JSONEachRow',
    kafka_num_consumers = 1,
    kafka_handle_error_mode = 'stream';

-- MergeTree storage table
CREATE TABLE IF NOT EXISTS keycloak.keycloak_admin_events (
    operationType    String,
    realmId          String,
    resourceType     String,
    resourcePath     String,
    authRealmId      String,
    authClientId     String,
    authUserId       String,
    authIpAddress    String,
    event_time       DateTime64(3) DEFAULT now64(3),
    error            String,
    representation   String,
    inserted_at      DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (realmId, operationType, event_time)
PARTITION BY toYYYYMM(event_time);

-- Materialized view: pipes data from Kafka engine → MergeTree
CREATE MATERIALIZED VIEW IF NOT EXISTS keycloak.keycloak_admin_events_mv
TO keycloak.keycloak_admin_events AS
SELECT
    operationType,
    realmId,
    resourceType,
    resourcePath,
    authRealmId,
    authClientId,
    authUserId,
    authIpAddress,
    fromUnixTimestamp64Milli(`time`) AS event_time,
    error,
    representation
FROM keycloak.keycloak_admin_events_kafka;

-- ==========================================================================
-- 3. Sample analytics views
-- ==========================================================================

-- Events per type per day
CREATE VIEW IF NOT EXISTS keycloak.events_per_day AS
SELECT
    toDate(event_time) AS day,
    type,
    realmId,
    count()            AS event_count
FROM keycloak.keycloak_events
GROUP BY day, type, realmId
ORDER BY day DESC, event_count DESC;

-- Login failures per user
CREATE VIEW IF NOT EXISTS keycloak.login_failures AS
SELECT
    userId,
    clientId,
    realmId,
    ipAddress,
    count()            AS failure_count,
    max(event_time)    AS last_failure
FROM keycloak.keycloak_events
WHERE type = 'LOGIN_ERROR'
GROUP BY userId, clientId, realmId, ipAddress
ORDER BY failure_count DESC;

-- Active users per day
CREATE VIEW IF NOT EXISTS keycloak.active_users_per_day AS
SELECT
    toDate(event_time) AS day,
    realmId,
    uniqExact(userId)  AS unique_users
FROM keycloak.keycloak_events
WHERE type = 'LOGIN'
GROUP BY day, realmId
ORDER BY day DESC;

