SELECT title_type AS 类型, COUNT(id) AS 电影数量
FROM titles
GROUP BY title_type
ORDER BY 电影数量 DESC
