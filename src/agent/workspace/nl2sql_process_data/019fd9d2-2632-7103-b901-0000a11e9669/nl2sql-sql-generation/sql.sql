WITH actor_works AS (
    SELECT p.name_id, AVG(t.average_rating) AS actor_avg_rating, COUNT(*) AS actor_movie_count
    FROM principals p
    JOIN titles t ON p.title_id = t.id
    WHERE p.category_id IN ('actor', 'actress')
      AND t.title_type = 'movie'
      AND t.average_rating IS NOT NULL
    GROUP BY p.name_id
    HAVING COUNT(*) >= 3
),
director_works AS (
    SELECT p.name_id, AVG(t.average_rating) AS director_avg_rating, COUNT(*) AS director_movie_count
    FROM principals p
    JOIN titles t ON p.title_id = t.id
    WHERE p.category_id = 'director'
      AND t.title_type = 'movie'
      AND t.average_rating IS NOT NULL
    GROUP BY p.name_id
    HAVING COUNT(*) >= 3
)
SELECT
    n.primary_name,
    a.actor_movie_count,
    ROUND(a.actor_avg_rating, 2) AS actor_avg_rating,
    d.director_movie_count,
    ROUND(d.director_avg_rating, 2) AS director_avg_rating
FROM actor_works a
JOIN director_works d ON a.name_id = d.name_id
JOIN names n ON a.name_id = n.id
