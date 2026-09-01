-- 删除 "The Ballad of Conrad & Vernon" (titles.id = tt10079494)
-- 按子表优先顺序删除关联记录，最后删除主记录

-- 1. 删除 principals_characters（经 principals 间接关联）
DELETE FROM principals_characters
WHERE principal_id IN (SELECT id FROM principals WHERE title_id = 'tt10079494');

-- 2. 删除 principals
DELETE FROM principals WHERE title_id = 'tt10079494';

-- 3. 删除 titleakas_titleakaattributes（经 titleakas 间接关联）
DELETE FROM titleakas_titleakaattributes
WHERE titleaka_id IN (SELECT id FROM titleakas WHERE title_id = 'tt10079494');

-- 4. 删除 titleakas_titleakatypes（经 titleakas 间接关联）
DELETE FROM titleakas_titleakatypes
WHERE titleaka_id IN (SELECT id FROM titleakas WHERE title_id = 'tt10079494');

-- 5. 删除 titleakas
DELETE FROM titleakas WHERE title_id = 'tt10079494';

-- 6. 删除 titles_genres
DELETE FROM titles_genres WHERE title_id = 'tt10079494';

-- 7. 删除 episodes（该作品作为剧集，id = tt10079494；注意：不删除父系列 tt0239181）
DELETE FROM episodes WHERE id = 'tt10079494';

-- 8. 删除 names_knownfortitles（引用该作品的代表作记录）
DELETE FROM names_knownfortitles WHERE title_id = 'tt10079494';

-- 9. 删除 titles 主记录
DELETE FROM titles WHERE id = 'tt10079494';
