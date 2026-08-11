SELECT 
    ROUND(AVG(average_rating), 2) AS avg_rating
FROM titles
WHERE title_type = 'movie'
    AND start_year BETWEEN 1990 AND 1999
    AND average_rating IS NOT NULL
