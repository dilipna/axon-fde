-- Seed data for the Axon ERP.
--
-- Small but not trivial: 10 vehicles, 10 shipments, 3 cold-storage facilities,
-- maintenance history and prior incidents. Enough for realistic retrieval,
-- small enough to reason about by hand.
--
-- Two details are load-bearing for the flagship demo:
--   * AX-042 has a prior AL17 compressor warning, so the fault code the
--     simulation raises has corroborating history.
--   * SH-2041's ERP envelope says 2-10 C while its signed Bill of Lading says
--     2-8 C. Nothing in Axon's current process notices.

USE AxonERP;
GO

DELETE FROM dbo.historical_incidents;
DELETE FROM dbo.maintenance_events;
DELETE FROM dbo.shipment_documents;
DELETE FROM dbo.cargo_requirements;
DELETE FROM dbo.route_stops;
DELETE FROM dbo.shipments;
DELETE FROM dbo.routes;
DELETE FROM dbo.facilities;
DELETE FROM dbo.drivers;
DELETE FROM dbo.vehicles;
DELETE FROM dbo.customers;
GO

INSERT INTO dbo.customers (customer_id, name, tier, notification_contact, contract_rate_usd) VALUES
 ('CU-001', N'Meridian Pharmaceuticals', 'strategic', N'ops@meridian-pharma.example',  4850.00),
 ('CU-002', N'Northlake Biologics',      'strategic', N'logistics@northlake.example',  5200.00),
 ('CU-003', N'Harvest Fresh Foods',      'standard',  N'inbound@harvestfresh.example', 1850.00),
 ('CU-004', N'Glacier Frozen Goods',     'standard',  N'dc@glacierfrozen.example',     2100.00);
GO

INSERT INTO dbo.vehicles (vehicle_id, reefer_model, trailer_class, in_service_date, odometer_km) VALUES
 ('AX-001', 'ThermoKing TK-500', 'reefer_53ft', '2021-03-14', 412300),
 ('AX-007', 'ThermoKing TK-500', 'reefer_53ft', '2022-06-02', 268900),
 ('AX-012', 'Carrier CX-40',     'reefer_53ft', '2020-11-20', 531200),
 ('AX-019', 'ThermoKing TK-500', 'reefer_53ft', '2021-08-09', 389400),
 ('AX-023', 'Carrier CX-40',     'reefer_48ft', '2023-01-17', 142700),
 ('AX-031', 'ThermoKing TK-500', 'reefer_53ft', '2022-02-28', 301500),
 ('AX-038', 'Carrier CX-40',     'reefer_53ft', '2020-05-11', 604800),
 ('AX-042', 'ThermoKing TK-500', 'reefer_53ft', '2019-09-30', 688200),
 ('AX-047', 'ThermoKing TK-500', 'reefer_48ft', '2023-04-22', 98300),
 ('AX-055', 'Carrier CX-40',     'reefer_53ft', '2021-12-05', 355100);
GO

INSERT INTO dbo.drivers (driver_id, name, phone, home_address, certifications) VALUES
 ('DR-101', N'M. Okonkwo',  '+1-312-555-0141', N'Chicago, IL',   'cold_chain,hazmat'),
 ('DR-102', N'S. Ramirez',  '+1-312-555-0182', N'Joliet, IL',    'cold_chain'),
 ('DR-103', N'J. Whitfield','+1-614-555-0119', N'Columbus, OH',  'cold_chain,pharma'),
 ('DR-104', N'A. Novak',    '+1-414-555-0173', N'Milwaukee, WI', 'cold_chain'),
 ('DR-105', N'T. Bergstrom','+1-317-555-0165', N'Indianapolis, IN', 'cold_chain,pharma');
GO

INSERT INTO dbo.facilities (facility_id, name, latitude, longitude, capabilities, capacity_slots, slots_in_use) VALUES
 ('CS-11', N'Gary Cold Storage',        41.593200, -87.346200, 'cold_storage,pharma_certified,trailer_swap', 12, 9),
 ('CS-12', N'Fort Wayne Cold Chain',    41.079800, -85.139400, 'cold_storage,pharma_certified',             18, 7),
 ('CS-13', N'Dayton Refrigerated Depot',39.758900, -84.191600, 'cold_storage,trailer_swap',                 10, 10);
GO

INSERT INTO dbo.routes (route_id, origin, destination, planned_duration_min) VALUES
 ('RT-01', N'Chicago, IL',    N'Columbus, OH',      330),
 ('RT-02', N'Milwaukee, WI',  N'Indianapolis, IN',  295),
 ('RT-03', N'Chicago, IL',    N'Cleveland, OH',     360),
 ('RT-04', N'Indianapolis, IN', N'Detroit, MI',     250);
GO

INSERT INTO dbo.route_stops (route_id, sequence, facility_id, planned_arrival) VALUES
 ('RT-01', 1, 'CS-11', '2026-07-14T11:20:00'),
 ('RT-01', 2, 'CS-12', '2026-07-14T13:05:00'),
 ('RT-02', 1, 'CS-12', '2026-07-14T12:40:00'),
 ('RT-03', 1, 'CS-11', '2026-07-14T11:50:00'),
 ('RT-04', 1, 'CS-13', '2026-07-14T12:10:00');
GO

INSERT INTO dbo.shipments (shipment_id, customer_id, vehicle_id, driver_id, route_id, status, cargo_value_usd, departed_at, planned_arrival) VALUES
 ('SH-2041', 'CU-001', 'AX-042', 'DR-103', 'RT-01', 'in_transit', 184000.00, '2026-07-14T10:00:00', '2026-07-14T15:30:00'),
 ('SH-2052', 'CU-001', 'AX-007', 'DR-101', 'RT-03', 'in_transit', 142000.00, '2026-07-14T10:00:00', '2026-07-14T16:00:00'),
 ('SH-2068', 'CU-002', 'AX-019', 'DR-105', 'RT-02', 'in_transit', 196000.00, '2026-07-14T10:00:00', '2026-07-14T14:55:00'),
 ('SH-2071', 'CU-002', 'AX-047', 'DR-103', 'RT-04', 'in_transit', 312000.00, '2026-07-14T09:30:00', '2026-07-14T13:40:00'),
 ('SH-2083', 'CU-003', 'AX-012', 'DR-102', 'RT-01', 'in_transit',  34500.00, '2026-07-14T08:15:00', '2026-07-14T13:45:00'),
 ('SH-2090', 'CU-003', 'AX-023', 'DR-104', 'RT-02', 'in_transit',  28900.00, '2026-07-14T08:45:00', '2026-07-14T13:40:00'),
 ('SH-2104', 'CU-003', 'AX-031', 'DR-101', 'RT-03', 'delivered',   41200.00, '2026-07-13T07:00:00', '2026-07-13T13:00:00'),
 ('SH-2115', 'CU-004', 'AX-038', 'DR-102', 'RT-04', 'in_transit',  62400.00, '2026-07-14T09:00:00', '2026-07-14T13:10:00'),
 ('SH-2122', 'CU-004', 'AX-055', 'DR-104', 'RT-01', 'in_transit',  58700.00, '2026-07-14T09:15:00', '2026-07-14T14:45:00'),
 ('SH-2130', 'CU-004', 'AX-001', 'DR-105', 'RT-02', 'in_transit',  71300.00, '2026-07-14T09:40:00', '2026-07-14T14:35:00');
GO

INSERT INTO dbo.cargo_requirements (shipment_id, cargo_class, permitted_temp_min_c, permitted_temp_max_c, special_handling) VALUES
 -- The ERP says 2-10 C. The signed Bill of Lading (SD-9001) says 2-8 C.
 -- The BOL is the legally operative document; nothing today reconciles them.
 ('SH-2041', 'pharma_2_8',      2.00,  10.00, N'upright,no_stacking'),
 ('SH-2052', 'pharma_2_8',      2.00,   8.00, N'upright'),
 ('SH-2068', 'pharma_2_8',      2.00,   8.00, N'upright,no_stacking'),
 ('SH-2071', 'vaccine_2_8',     2.00,   8.00, N'upright,continuous_monitoring'),
 ('SH-2083', 'fresh_0_4',       0.00,   4.00, NULL),
 ('SH-2090', 'fresh_0_4',       0.00,   4.00, NULL),
 ('SH-2104', 'fresh_0_4',       0.00,   4.00, NULL),
 ('SH-2115', 'frozen_minus_18',-22.00, -18.00, NULL),
 ('SH-2122', 'frozen_minus_18',-22.00, -18.00, NULL),
 ('SH-2130', 'frozen_minus_18',-22.00, -18.00, NULL);
GO

INSERT INTO dbo.shipment_documents (document_id, shipment_id, doc_type, storage_uri, signed_at) VALUES
 ('SD-9001', 'SH-2041', 'bill_of_lading', 's3://axon-artifacts/bol/SH-2041.pdf', '2026-07-14T09:42:00'),
 ('SD-9002', 'SH-2052', 'bill_of_lading', 's3://axon-artifacts/bol/SH-2052.pdf', '2026-07-14T09:31:00'),
 ('SD-9003', 'SH-2068', 'bill_of_lading', 's3://axon-artifacts/bol/SH-2068.pdf', '2026-07-14T09:18:00'),
 ('SD-9004', 'SH-2071', 'bill_of_lading', 's3://axon-artifacts/bol/SH-2071.pdf', '2026-07-14T09:05:00'),
 ('SD-9005', 'SH-2041', 'manifest',       's3://axon-artifacts/manifest/SH-2041.pdf', NULL);
GO

INSERT INTO dbo.maintenance_events (event_id, vehicle_id, occurred_at, event_type, fault_codes, severity, technician_notes) VALUES
 -- The history that corroborates AL17 when the simulation raises it.
 ('ME-5001', 'AX-042', '2026-06-18T14:20:00', 'warning',    'AL17', 'warning',  N'Compressor discharge pressure trending high under load. Monitor; recommend service within 60 days.'),
 ('ME-5002', 'AX-042', '2026-05-02T09:10:00', 'service',    NULL,   'info',     N'Routine PM. Refrigerant topped up, filters replaced.'),
 ('ME-5003', 'AX-042', '2026-03-11T16:45:00', 'repair',     'AL17', 'warning',  N'Replaced condenser fan relay after intermittent AL17.'),
 ('ME-5004', 'AX-007', '2026-06-25T11:00:00', 'service',    NULL,   'info',     N'Routine PM. No faults found.'),
 ('ME-5005', 'AX-019', '2026-06-30T08:30:00', 'inspection', NULL,   'info',     N'Annual inspection passed. Sensor calibration within tolerance.'),
 ('ME-5006', 'AX-012', '2026-06-12T13:15:00', 'warning',    'AL31', 'warning',  N'Reefer fuel sender reading erratic below quarter tank.'),
 ('ME-5007', 'AX-038', '2026-05-28T10:05:00', 'repair',     'AL02', 'critical', N'Rear door seal replaced after repeated open-door alarms.'),
 ('ME-5008', 'AX-055', '2026-06-20T15:40:00', 'service',    NULL,   'info',     N'Routine PM completed.'),
 ('ME-5009', 'AX-031', '2026-04-14T09:55:00', 'service',    NULL,   'info',     N'Routine PM completed.'),
 ('ME-5010', 'AX-023', '2026-06-05T12:25:00', 'inspection', NULL,   'info',     N'Pre-season reefer inspection passed.'),
 ('ME-5011', 'AX-047', '2026-06-28T14:00:00', 'service',    NULL,   'info',     N'Routine PM. New unit, no issues.'),
 ('ME-5012', 'AX-001', '2026-05-19T11:30:00', 'warning',    'AL17', 'info',     N'Single AL17 event, not reproduced. No action taken.');
GO

INSERT INTO dbo.historical_incidents (incident_id, vehicle_id, occurred_at, symptom_summary, root_cause, intervention, outcome) VALUES
 ('HI-3001', 'AX-042', '2026-03-11T15:30:00', N'Gradual temperature rise with AL17 present; ambient 31C',    'compressor_degradation', 'reroute_to_cold_storage', 'success'),
 ('HI-3002', 'AX-012', '2026-02-22T10:15:00', N'Temperature spike then recovery; door alarm active',          'door_left_open',         'contact_driver',          'success'),
 ('HI-3003', 'AX-038', '2026-05-28T09:20:00', N'Repeated open-door alarms with stable cargo temperature',     'sensor_malfunction',     'mark_for_inspection',     'success'),
 ('HI-3004', 'AX-055', '2026-01-08T13:45:00', N'Slow rise during heatwave, unit running continuously',        'environmental_heat',     'reroute_to_cold_storage', 'partial'),
 ('HI-3005', 'AX-001', '2026-05-19T12:10:00', N'Brief AL17, temperature unaffected',                          'compressor_degradation', 'continue_route',          'success'),
 ('HI-3006', 'AX-031', '2025-11-30T08:05:00', N'Reefer shut down, fuel exhausted mid-route',                  'reefer_fuel_exhaustion', 'contact_driver',          'failure'),
 ('HI-3007', 'AX-019', '2025-10-17T16:20:00', N'Reported excursion contradicted by manual probe reading',     'sensor_malfunction',     'mark_for_inspection',     'success'),
 ('HI-3008', 'AX-007', '2025-09-03T11:55:00', N'Temperature drift during extended traffic delay',             'route_delay',            'reroute_to_cold_storage', 'success'),
 ('HI-3009', 'AX-042', '2025-08-21T14:10:00', N'Compressor cycling irregularly, cargo within range',          'compressor_degradation', 'escalate_maintenance',    'success'),
 ('HI-3010', 'AX-023', '2025-07-29T10:40:00', N'Cooling loss traced to blocked evaporator',                   'compressor_degradation', 'trailer_swap',            'success'),
 ('HI-3011', 'AX-047', '2026-06-02T09:30:00', N'False excursion alert during loading dock transfer',          'sensor_malfunction',     'continue_route',          'success'),
 ('HI-3012', 'AX-038', '2025-12-14T07:15:00', N'Frozen load approaching upper limit in transit',              'environmental_heat',     'reroute_to_cold_storage', 'success');
GO
