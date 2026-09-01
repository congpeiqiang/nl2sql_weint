import psycopg2

conn = psycopg2.connect(
    host="8.163.4.42",
    port=5432,
    user="chinook",
    password="chinook123",
    dbname="chinook"
)

cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM artist")
result = cur.fetchone()
print(f"artist_count: {result[0]}")

cur.close()
conn.close()
