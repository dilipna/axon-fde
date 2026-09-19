-- The AI-facing surface of the legacy system.
--
-- Six views. Every one is an explicit column projection, never SELECT *.
-- Columns the AI has no business seeing never leave the database, so no
-- downstream mistake can expose them. When a new column appears on a base
-- table it is invisible here until someone deliberately adds it, which is the
-- safe default.
--
-- Deliberately excluded:
--   customers.contract_rate_usd  commercially sensitive pricing
--   drivers.phone, home_address  personal data
--
-- See docs/adr/005-read-only-legacy-access.md.

USE AxonERP;
GO

DROP VIEW IF EXISTS dbo.vw_ai_shipments;
DROP VIEW IF EXISTS dbo.vw_ai_vehicles;
DROP VIEW IF EXISTS dbo.vw_ai_cargo_requirements;
DROP VIEW IF EXISTS dbo.vw_ai_maintenance;
DROP VIEW IF EXISTS dbo.vw_ai_facilities;
DROP VIEW IF EXISTS dbo.vw_ai_historical_incidents;
GO

CREATE VIEW dbo.vw_ai_shipments AS
SELECT
    s.shipment_id,
    s.customer_id,
    c.name          AS customer_name,
    c.tier          AS customer_tier,
    s.vehicle_id,
    s.driver_id,
    d.name          AS driver_name,          -- name only; no phone, no address
    d.certifications AS driver_certifications,
    s.route_id,
    r.origin,
    r.destination,
    r.planned_duration_min,
    s.status,
    s.cargo_value_usd,
    s.departed_at,
    s.planned_arrival
FROM dbo.shipments s
JOIN dbo.customers c ON c.customer_id = s.customer_id
JOIN dbo.drivers   d ON d.driver_id   = s.driver_id
JOIN dbo.routes    r ON r.route_id    = s.route_id;
GO

CREATE VIEW dbo.vw_ai_vehicles AS
SELECT
    v.vehicle_id,
    v.reefer_model,
    v.trailer_class,
    v.in_service_date,
    v.odometer_km,
    DATEDIFF(DAY, v.in_service_date, SYSUTCDATETIME()) AS days_in_service
FROM dbo.vehicles v;
GO

CREATE VIEW dbo.vw_ai_cargo_requirements AS
SELECT
    cr.shipment_id,
    cr.cargo_class,
    cr.permitted_temp_min_c,
    cr.permitted_temp_max_c,
    cr.special_handling
FROM dbo.cargo_requirements cr;
GO

CREATE VIEW dbo.vw_ai_maintenance AS
SELECT
    m.event_id,
    m.vehicle_id,
    m.occurred_at,
    m.event_type,
    m.fault_codes,
    m.severity,
    m.technician_notes,
    DATEDIFF(DAY, m.occurred_at, SYSUTCDATETIME()) AS days_ago
FROM dbo.maintenance_events m;
GO

CREATE VIEW dbo.vw_ai_facilities AS
SELECT
    f.facility_id,
    f.name,
    f.latitude,
    f.longitude,
    f.capabilities,
    f.capacity_slots,
    f.slots_in_use,
    (f.capacity_slots - f.slots_in_use) AS slots_available
FROM dbo.facilities f;
GO

CREATE VIEW dbo.vw_ai_historical_incidents AS
SELECT
    h.incident_id,
    h.vehicle_id,
    h.occurred_at,
    h.symptom_summary,
    h.root_cause,
    h.intervention,
    h.outcome
FROM dbo.historical_incidents h;
GO
