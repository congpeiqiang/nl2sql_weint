#!/bin/bash
# Chinook PostgreSQL: grants + remote access config + verification
set -e
cd /tmp

echo "== grants for chinook role =="
sudo -u postgres psql -d chinook -c "GRANT USAGE ON SCHEMA public TO chinook;"
sudo -u postgres psql -d chinook -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO chinook;"
sudo -u postgres psql -d chinook -c "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO chinook;"
sudo -u postgres psql -d chinook -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO chinook;"

echo "== remote access =="
PG_CONF=$(ls /etc/postgresql/*/main/postgresql.conf)
PG_HBA=$(ls /etc/postgresql/*/main/pg_hba.conf)
sudo sed -i "s/^#\?listen_addresses.*/listen_addresses = '*'/" "$PG_CONF"
if ! sudo grep -q "host.*all.*chinook" "$PG_HBA"; then
  sudo bash -c "cat >> '$PG_HBA' <<'EOF'

# chinook remote access (password / scram-sha-256)
host    all             chinook         0.0.0.0/0               scram-sha-256
host    all             chinook         ::0/0                   scram-sha-256
EOF"
fi
sudo systemctl restart postgresql
sleep 2

echo "== listening check =="
sudo ss -tlnp 2>/dev/null | grep 5432 || echo "no listener on 5432?"

echo "== local password-login test =="
PGPASSWORD=chinook123 psql -h 127.0.0.1 -U chinook -d chinook -c "SELECT current_user, version();" 2>&1 | head -4

echo "== verification queries =="
sudo -u postgres psql -d chinook -c "SELECT Name FROM Artist ORDER BY ArtistId LIMIT 5;"
sudo -u postgres psql -d chinook -c "SELECT a.Title, ar.Name FROM Album a JOIN Artist ar ON a.ArtistId = ar.ArtistId WHERE ar.Name = 'AC/DC' LIMIT 3;"
sudo -u postgres psql -d chinook -c "SELECT c.FirstName, c.LastName, SUM(i.Total) AS total_spent FROM Customer c JOIN Invoice i ON c.CustomerId = i.CustomerId GROUP BY c.CustomerId ORDER BY total_spent DESC LIMIT 5;"
sudo -u postgres psql -d chinook -c "SELECT FirstName, LastName, Email FROM Customer WHERE CustomerId NOT IN (SELECT DISTINCT CustomerId FROM Invoice);"
sudo -u postgres psql -d chinook -c "SELECT a.Title, ar.Name, SUM(CASE WHEN t.UnitPrice = 0.99 THEN 1 ELSE 0 END) AS cnt_099, SUM(CASE WHEN t.UnitPrice = 1.99 THEN 1 ELSE 0 END) AS cnt_199 FROM Album a JOIN Artist ar ON a.ArtistId = ar.ArtistId JOIN Track t ON a.AlbumId = t.AlbumId GROUP BY a.AlbumId, a.Title, ar.Name HAVING COUNT(DISTINCT t.UnitPrice) > 1 LIMIT 5;"

echo "CONFIG_DONE"
