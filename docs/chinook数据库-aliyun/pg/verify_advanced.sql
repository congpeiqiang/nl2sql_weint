-- Q29 re-check: albums mixing 0.99 and 1.99
SELECT a.Title, ar.Name,
       SUM(CASE WHEN t.UnitPrice = 0.99 THEN 1 ELSE 0 END) AS cnt_099,
       SUM(CASE WHEN t.UnitPrice = 1.99 THEN 1 ELSE 0 END) AS cnt_199
FROM Album a
JOIN Artist ar ON a.ArtistId = ar.ArtistId
JOIN Track t ON a.AlbumId = t.AlbumId
GROUP BY a.AlbumId, a.Title, ar.Name
HAVING COUNT(DISTINCT t.UnitPrice) > 1
ORDER BY cnt_199 DESC LIMIT 5;

-- Albums containing 1.99 tracks (sanity)
SELECT a.Title, COUNT(*) AS n_199
FROM Album a JOIN Track t ON a.AlbumId = t.AlbumId
WHERE t.UnitPrice = 1.99 GROUP BY a.AlbumId, a.Title ORDER BY n_199 DESC LIMIT 5;

-- Q26: top customer spend per genre (window function)
SELECT * FROM (
  SELECT g.Name AS genre, c.FirstName, c.LastName,
         SUM(il.UnitPrice * il.Quantity) AS spend,
         ROW_NUMBER() OVER (PARTITION BY g.GenreId ORDER BY SUM(il.UnitPrice * il.Quantity) DESC) AS rn
  FROM Genre g
  JOIN Track t ON t.GenreId = g.GenreId
  JOIN InvoiceLine il ON il.TrackId = t.TrackId
  JOIN Invoice i ON i.InvoiceId = il.InvoiceId
  JOIN Customer c ON c.CustomerId = i.CustomerId
  GROUP BY g.GenreId, g.Name, c.CustomerId, c.FirstName, c.LastName
) x WHERE rn = 1 ORDER BY spend DESC LIMIT 5;

-- Q27: customers who bought ALL Jazz tracks (division)
SELECT FirstName, LastName FROM Customer c
WHERE NOT EXISTS (
  SELECT t.TrackId FROM Track t
  JOIN Genre g ON t.GenreId = g.GenreId
  WHERE g.Name = 'Jazz'
  AND NOT EXISTS (
    SELECT 1 FROM Invoice i
    JOIN InvoiceLine il ON i.InvoiceId = il.InvoiceId
    WHERE i.CustomerId = c.CustomerId AND il.TrackId = t.TrackId
  )
);
