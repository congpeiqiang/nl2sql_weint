SELECT
    id,
    device_code,
    collect_time,
    puller_batch_no,
    ingot_no,
    work_order_no,
    proc_stage,
    melt_bright,
    melt_time,
    melt_time_seconds,
    melt_depth
FROM aix_crystal_comm_data
WHERE proc_stage = '预热|熔接'
