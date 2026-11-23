-- Runs once, when the Postgres volume is first created. Each service keeps its tables in its own schema.
CREATE SCHEMA IF NOT EXISTS keycloak;
CREATE SCHEMA IF NOT EXISTS channel;
