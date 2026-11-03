-- Post-migration updates for application server / PXE hostnames.
-- Placeholders (filled from config post_migration section):
--   {{APP_SERVER_HOST}}     new PostgreSQL application server host
--   {{PXE_SERVER_HOST}}     new PXE server host
--   {{OLD_APP_SERVER_HOST}} hostname from source Oracle/SQL Server app server

update application_server set
host_name = '{{APP_SERVER_HOST}}',
app_server_name = '{{APP_SERVER_HOST}}',
display_name = '{{APP_SERVER_HOST}}'
where
host_name = '{{OLD_APP_SERVER_HOST}}';

/* update application_server set
host_name = '{{APP_SERVER_HOST}}',
app_server_name = '{{PXE_SERVER_HOST}}',
display_name = '{{PXE_SERVER_HOST}}'
where
application_server_id = 2; */

update application_server_model set
name = '{{APP_SERVER_HOST}}',
display_name = '{{APP_SERVER_HOST}}'
where
name = '{{OLD_APP_SERVER_HOST}}';

/* update application_server_model set
name = '{{PXE_SERVER_HOST}}',
display_name = '{{PXE_SERVER_HOST}}'
where
application_server_model_id = 2; */

update application_server_deployments
set host_name = '{{APP_SERVER_HOST}}';

update service_config_property_value
set config_property_value = '{{APP_SERVER_HOST}}'
where config_property_value = '{{OLD_APP_SERVER_HOST}}';

update property_set_instance
set name = '{{APP_SERVER_HOST}}'
where name = '{{OLD_APP_SERVER_HOST}}';

update primitive_bl_value
set value = '{{APP_SERVER_HOST}}'
where value = '{{OLD_APP_SERVER_HOST}}';

-- Leave as-is (not hostname-related)
update system_property
set value = 'Postgres'
where name = 'DatabaseType';
