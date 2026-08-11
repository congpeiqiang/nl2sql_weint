-- 子查询1：电视剧系列总数
SELECT COUNT(*) AS total_series
FROM titles
WHERE title_type = 'tvSeries';

-- 子查询2：按类型（genre）统计电视剧系列数量分布
SELECT g.display_name AS genre, COUNT(DISTINCT t.id) AS series_count
FROM titles t
JOIN titles_genres tg ON t.id = tg.title_id
JOIN genres g ON tg.genre_id = g.id
WHERE t.title_type = 'tvSeries'
GROUP BY g.display_name
ORDER BY series_count DESC;

-- 子查询3：按首播年份统计电视剧系列数量趋势
SELECT t.start_year, COUNT(*) AS series_count
FROM titles t
WHERE t.title_type = 'tvSeries' AND t.start_year IS NOT NULL
GROUP BY t.start_year
ORDER BY t.start_year ASC;
