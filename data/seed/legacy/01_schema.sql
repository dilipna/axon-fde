-- Axon ERP: the legacy system of record.
--
-- This schema is NOT ours. We never migrate it and never write to it. It is
-- seeded here so the integration has something real to read, and is managed
-- with raw T-SQL rather than Alembic precisely to keep that asymmetry visible.
-- Alembic manages only our own PostgreSQL.

IF DB_ID('AxonERP') IS NULL
    CREATE DATABASE AxonERP;
GO

USE AxonERP;
GO

-- Idempotent: the seed can be re-run without a fresh container.
DROP TABLE IF EXISTS dbo.historical_incidents;
DROP TABLE IF EXISTS dbo.maintenance_events;
DROP TABLE IF EXISTS dbo.shipment_documents;
DROP TABLE IF EXISTS dbo.cargo_requirements;
DROP TABLE IF EXISTS dbo.route_stops;
DROP TABLE IF EXISTS dbo.shipments;
DROP TABLE IF EXISTS dbo.routes;
DROP TABLE IF EXISTS dbo.facilities;
DROP TABLE IF EXISTS dbo.drivers;
DROP TABLE IF EXISTS dbo.vehicles;
DROP TABLE IF EXISTS dbo.customers;
GO

CREATE TABLE dbo.customers (
    customer_id          VARCHAR(16)   NOT NULL PRIMARY KEY,
    name                 NVARCHAR(128) NOT NULL,
    tier                 VARCHAR(16)   NOT NULL,  -- strategic | standard
    notification_contact NVARCHAR(128) NOT NULL,
    -- Commercially sensitive. Deliberately excluded from every vw_ai_* view:
    -- the AI has no business reason to see contract pricing.
    contract_rate_usd    DECIMAL(10,2) NOT NULL
);

CREATE TABLE dbo.vehicles (
    vehicle_id      VARCHAR(16)  NOT NULL PRIMARY KEY,
    reefer_model    VARCHAR(32)  NOT NULL,
    trailer_class   VARCHAR(32)  NOT NULL,
    in_service_date DATE         NOT NULL,
    odometer_km     INT          NOT NULL
);

CREATE TABLE dbo.drivers (
    driver_id      VARCHAR(16)   NOT NULL PRIMARY KEY,
    name           NVARCHAR(128) NOT NULL,
    -- Personal data. Excluded from the AI-facing views.
    phone          VARCHAR(32)   NOT NULL,
    home_address   NVARCHAR(256) NULL,
    certifications VARCHAR(128)  NOT NULL
);

CREATE TABLE dbo.facilities (
    facility_id  VARCHAR(16)   NOT NULL PRIMARY KEY,
    name         NVARCHAR(128) NOT NULL,
    latitude     DECIMAL(9,6)  NOT NULL,
    longitude    DECIMAL(9,6)  NOT NULL,
    capabilities VARCHAR(128)  NOT NULL,  -- comma separated
    capacity_slots INT         NOT NULL,
    slots_in_use   INT         NOT NULL
);

CREATE TABLE dbo.routes (
    route_id              VARCHAR(16)   NOT NULL PRIMARY KEY,
    origin                NVARCHAR(128) NOT NULL,
    destination           NVARCHAR(128) NOT NULL,
    planned_duration_min  INT           NOT NULL
);

CREATE TABLE dbo.shipments (
    shipment_id     VARCHAR(16)  NOT NULL PRIMARY KEY,
    customer_id     VARCHAR(16)  NOT NULL REFERENCES dbo.customers(customer_id),
    vehicle_id      VARCHAR(16)  NOT NULL REFERENCES dbo.vehicles(vehicle_id),
    driver_id       VARCHAR(16)  NOT NULL REFERENCES dbo.drivers(driver_id),
    route_id        VARCHAR(16)  NOT NULL REFERENCES dbo.routes(route_id),
    status          VARCHAR(24)  NOT NULL,
    cargo_value_usd DECIMAL(12,2) NOT NULL,
    departed_at     DATETIME2    NOT NULL,
    planned_arrival DATETIME2    NOT NULL
);

CREATE TABLE dbo.cargo_requirements (
    shipment_id          VARCHAR(16)  NOT NULL PRIMARY KEY
                         REFERENCES dbo.shipments(shipment_id),
    cargo_class          VARCHAR(32)  NOT NULL,
    -- The ERP's view of the permitted envelope. For SH-2041 this deliberately
    -- disagrees with the signed Bill of Lading, which is the conflict the
    -- reconciliation engine exists to catch.
    permitted_temp_min_c DECIMAL(5,2) NOT NULL,
    permitted_temp_max_c DECIMAL(5,2) NOT NULL,
    special_handling     VARCHAR(128) NULL
);

CREATE TABLE dbo.route_stops (
    route_id      VARCHAR(16) NOT NULL REFERENCES dbo.routes(route_id),
    sequence      INT         NOT NULL,
    facility_id   VARCHAR(16) NOT NULL REFERENCES dbo.facilities(facility_id),
    planned_arrival DATETIME2 NOT NULL,
    CONSTRAINT pk_route_stops PRIMARY KEY (route_id, sequence)
);

CREATE TABLE dbo.maintenance_events (
    event_id         VARCHAR(16)   NOT NULL PRIMARY KEY,
    vehicle_id       VARCHAR(16)   NOT NULL REFERENCES dbo.vehicles(vehicle_id),
    occurred_at      DATETIME2     NOT NULL,
    event_type       VARCHAR(32)   NOT NULL,  -- service | inspection | warning | repair
    fault_codes      VARCHAR(64)   NULL,      -- comma separated
    severity         VARCHAR(16)   NOT NULL,  -- info | warning | critical
    technician_notes NVARCHAR(512) NULL
);

CREATE TABLE dbo.shipment_documents (
    document_id  VARCHAR(16)   NOT NULL PRIMARY KEY,
    shipment_id  VARCHAR(16)   NOT NULL REFERENCES dbo.shipments(shipment_id),
    doc_type     VARCHAR(32)   NOT NULL,  -- bill_of_lading | manifest | inspection_form
    storage_uri  VARCHAR(256)  NOT NULL,
    signed_at    DATETIME2     NULL
);

CREATE TABLE dbo.historical_incidents (
    incident_id     VARCHAR(16)   NOT NULL PRIMARY KEY,
    vehicle_id      VARCHAR(16)   NOT NULL REFERENCES dbo.vehicles(vehicle_id),
    occurred_at     DATETIME2     NOT NULL,
    symptom_summary NVARCHAR(256) NOT NULL,
    root_cause      VARCHAR(48)   NOT NULL,
    intervention    VARCHAR(48)   NOT NULL,
    outcome         VARCHAR(24)   NOT NULL   -- success | partial | failure
);
GO

CREATE INDEX ix_maintenance_vehicle ON dbo.maintenance_events(vehicle_id, occurred_at DESC);
CREATE INDEX ix_shipments_vehicle   ON dbo.shipments(vehicle_id);
CREATE INDEX ix_incidents_vehicle   ON dbo.historical_incidents(vehicle_id, occurred_at DESC);
GO
