-- The least-privilege login the application uses.
--
-- This is the control that makes "the AI cannot write to the ERP" a fact
-- rather than an assurance.
--
-- The principle is default-deny: a new database user has no permissions at
-- all, so base tables are unreachable simply by never being granted. We then
-- grant SELECT on exactly six views, and add an explicit DENY on the write
-- verbs as a second layer in case a future change grants something broader.
--
-- Deliberately NOT used:
--   db_datareader   would grant every base table, including ones that do not
--                   exist yet, so the blast radius would grow silently with
--                   the customer's schema.
--   any write role  no INSERT, UPDATE, DELETE or DDL anywhere.
--   EXECUTE         no stored procedures, no xp_cmdshell.
--
-- A schema-level DENY SELECT is deliberately avoided: the views read their
-- base tables through ownership chaining, and a blanket DENY complicates that
-- for no gain when the base tables were never granted in the first place.
--
-- See docs/adr/005-read-only-legacy-access.md.
-- Integration tests assert every property claimed here.

USE AxonERP;
GO

-- The seed script substitutes the password before execution; the placeholder
-- is what lives in source control.
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'axon_ai_ro')
    CREATE LOGIN axon_ai_ro WITH PASSWORD = '{{RO_PASSWORD}}', CHECK_POLICY = OFF;
GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'axon_ai_ro')
    CREATE USER axon_ai_ro FOR LOGIN axon_ai_ro;
GO

-- Re-running the seed must not accumulate privilege.
IF EXISTS (SELECT 1 FROM sys.database_role_members rm
           JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
           JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id
           WHERE m.name = 'axon_ai_ro' AND r.name = 'db_datareader')
    ALTER ROLE db_datareader DROP MEMBER axon_ai_ro;
GO

IF EXISTS (SELECT 1 FROM sys.database_role_members rm
           JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
           JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id
           WHERE m.name = 'axon_ai_ro' AND r.name = 'db_datawriter')
    ALTER ROLE db_datawriter DROP MEMBER axon_ai_ro;
GO

-- Second layer: even if something later grants a broader SELECT, writes stay
-- impossible. DENY always wins over GRANT in SQL Server.
DENY INSERT, UPDATE, DELETE ON SCHEMA::dbo TO axon_ai_ro;
DENY EXECUTE ON SCHEMA::dbo TO axon_ai_ro;
GO

-- The entire AI-facing surface, enumerated.
GRANT SELECT ON dbo.vw_ai_shipments            TO axon_ai_ro;
GRANT SELECT ON dbo.vw_ai_vehicles             TO axon_ai_ro;
GRANT SELECT ON dbo.vw_ai_cargo_requirements   TO axon_ai_ro;
GRANT SELECT ON dbo.vw_ai_maintenance          TO axon_ai_ro;
GRANT SELECT ON dbo.vw_ai_facilities           TO axon_ai_ro;
GRANT SELECT ON dbo.vw_ai_historical_incidents TO axon_ai_ro;
GO
