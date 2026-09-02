SELECT DISTINCT UnitPrice FROM Track ORDER BY 1;
SELECT MIN(InvoiceDate)::date AS min_date, MAX(InvoiceDate)::date AS max_date FROM Invoice;
SELECT Name FROM Artist WHERE Name LIKE '%Motörhead%' OR Name LIKE '%Mötley%';
SELECT Name FROM Track WHERE Name LIKE '%Mendelssohn%' LIMIT 2;
SELECT last_value, is_called FROM album_AlbumId_seq;
SELECT PlaylistId, COUNT(*) AS tracks FROM PlaylistTrack GROUP BY PlaylistId ORDER BY PlaylistId LIMIT 5;
SELECT COUNT(*) AS total_invoices_2024 FROM Invoice WHERE EXTRACT(YEAR FROM InvoiceDate) = 2024;
