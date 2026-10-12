\getenv metadata_app_password METADATA_APP_PASSWORD
CREATE ROLE text2sql_app LOGIN PASSWORD :'metadata_app_password';
GRANT CONNECT ON DATABASE metadata TO text2sql_app;
