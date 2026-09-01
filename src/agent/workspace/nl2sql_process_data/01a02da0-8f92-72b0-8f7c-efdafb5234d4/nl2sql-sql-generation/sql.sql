SELECT
    ct.country AS country,
    ROUND(AVG(ct.total_spent), 2) AS avg_spend
FROM (
    SELECT
        c.customerid,
        c.country,
        SUM(i.total) AS total_spent
    FROM customer c
    LEFT JOIN invoice i ON c.customerid = i.customerid
    GROUP BY c.customerid, c.country
) ct
GROUP BY ct.country
ORDER BY avg_spend DESC
