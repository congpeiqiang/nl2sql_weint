#!/bin/bash
# Chinook PostgreSQL setup on Ubuntu (run as ecs-user with passwordless sudo)
set -e
cd /tmp

echo "== create role =="
sudo -u postgres psql -c "DROP DATABASE IF EXISTS chinook;" || true
sudo -u postgres psql -c "DROP ROLE IF EXISTS chinook;" || true
sudo -u postgres psql -c "CREATE ROLE chinook LOGIN PASSWORD 'chinook123';"
sudo -u postgres psql -c "CREATE DATABASE chinook OWNER chinook ENCODING 'UTF8';"

echo "== import schema =="
sudo -u postgres psql -d chinook -v ON_ERROR_STOP=1 -f /tmp/chinook_schema.sql

echo "== import data =="
sudo -u postgres PGCLIENTENCODING=UTF8 psql -d chinook -v ON_ERROR_STOP=1 -f /tmp/chinook_data.sql

echo "== verify tables =="
sudo -u postgres psql -d chinook -c "\dt"

echo "== verify row counts =="
sudo -u postgres psql -d chinook -c "SELECT 'Genre' AS tbl, count(*) AS n FROM Genre UNION ALL SELECT 'MediaType', count(*) FROM MediaType UNION ALL SELECT 'Artist', count(*) FROM Artist UNION ALL SELECT 'Album', count(*) FROM Album UNION ALL SELECT 'Track', count(*) FROM Track UNION ALL SELECT 'Employee', count(*) FROM Employee UNION ALL SELECT 'Customer', count(*) FROM Customer UNION ALL SELECT 'Invoice', count(*) FROM Invoice UNION ALL SELECT 'InvoiceLine', count(*) FROM InvoiceLine UNION ALL SELECT 'Playlist', count(*) FROM Playlist UNION ALL SELECT 'PlaylistTrack', count(*) FROM PlaylistTrack ORDER BY 1;"

echo "SETUP_DONE"
