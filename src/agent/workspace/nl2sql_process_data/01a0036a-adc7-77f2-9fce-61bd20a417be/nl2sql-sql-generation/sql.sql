SELECT
    collect_time,
    melt_bright,
    shoulder_rel_bright,
    proc_stage,
    puller_batch_no,
    ingot_no
FROM aix_crystal_comm_data
WHERE device_code = 'D330'
  AND proc_stage = '调温'
ORDER BY collect_time DESC