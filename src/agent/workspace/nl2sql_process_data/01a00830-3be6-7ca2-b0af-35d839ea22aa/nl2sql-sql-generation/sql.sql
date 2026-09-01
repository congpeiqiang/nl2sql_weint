-- 曲目明细查询（曲目名称、所属专辑、类型）
SELECT
  track.trackid  AS "曲目ID",
  track.name     AS "曲目名称",
  album.title    AS "所属专辑",
  genre.name     AS "类型",
  track.milliseconds AS "时长(毫秒)",
  track.unitprice    AS "单价"
FROM track
LEFT JOIN album ON track.albumid = album.albumid
LEFT JOIN genre ON track.genreid = genre.genreid
ORDER BY track.trackid ASC;

-- 曲目总数概览
SELECT COUNT(*) AS "曲目总数" FROM track;
