SELECT COUNT(*) AS tv_series_count
FROM titles
WHERE title_type = 'tvSeries' AND start_year >= 2020
