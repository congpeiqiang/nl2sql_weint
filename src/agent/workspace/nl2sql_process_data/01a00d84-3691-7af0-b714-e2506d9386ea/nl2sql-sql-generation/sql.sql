SELECT
    device_code,
    COUNT(*) AS cnt
FROM aix_crystal_record_data
WHERE content = '调温'
GROUP BY device_code
ORDER BY cnt DESC, device_code ASC
