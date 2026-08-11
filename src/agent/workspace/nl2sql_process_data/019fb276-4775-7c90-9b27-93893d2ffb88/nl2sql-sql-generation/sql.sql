SELECT 'categories' AS table_name, COUNT(*) AS row_count FROM categories
UNION ALL
SELECT 'episodes', COUNT(*) FROM episodes
UNION ALL
SELECT 'genres', COUNT(*) FROM genres
UNION ALL
SELECT 'jobs', COUNT(*) FROM jobs
UNION ALL
SELECT 'names', COUNT(*) FROM names
UNION ALL
SELECT 'names_knownfortitles', COUNT(*) FROM names_knownfortitles
UNION ALL
SELECT 'names_primaryprofessions', COUNT(*) FROM names_primaryprofessions
UNION ALL
SELECT 'principals', COUNT(*) FROM principals
UNION ALL
SELECT 'principals_characters', COUNT(*) FROM principals_characters
UNION ALL
SELECT 'professions', COUNT(*) FROM professions
UNION ALL
SELECT 'titleakaattributes', COUNT(*) FROM titleakaattributes
UNION ALL
SELECT 'titleakas', COUNT(*) FROM titleakas
UNION ALL
SELECT 'titleakas_titleakaattributes', COUNT(*) FROM titleakas_titleakaattributes
UNION ALL
SELECT 'titleakas_titleakatypes', COUNT(*) FROM titleakas_titleakatypes
UNION ALL
SELECT 'titleakatypes', COUNT(*) FROM titleakatypes
UNION ALL
SELECT 'titles', COUNT(*) FROM titles
UNION ALL
SELECT 'titles_genres', COUNT(*) FROM titles_genres
ORDER BY table_name;
