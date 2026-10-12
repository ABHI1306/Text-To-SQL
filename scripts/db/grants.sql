\getenv warehouse_reader_password WAREHOUSE_READER_PASSWORD
SELECT format(
    'CREATE ROLE warehouse_reader LOGIN PASSWORD %L',
    :'warehouse_reader_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'warehouse_reader'
)
\gexec
GRANT CONNECT ON DATABASE demo TO warehouse_reader;
GRANT USAGE ON SCHEMA ecommerce, human_resources TO warehouse_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA ecommerce, human_resources TO warehouse_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA ecommerce, human_resources
    GRANT SELECT ON TABLES TO warehouse_reader;
