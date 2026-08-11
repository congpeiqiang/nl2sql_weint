SELECT COUNT(DISTINCT title_type) AS tv_type_count
FROM titles
WHERE title_type LIKE 'tv%'
