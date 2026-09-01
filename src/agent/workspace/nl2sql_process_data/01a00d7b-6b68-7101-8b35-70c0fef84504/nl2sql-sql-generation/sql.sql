SELECT device_code, collect_time, operation_time, puller_batch_no, ingot_no, proc_stage, work_order_no, content, value, color
FROM aix_crystal_record_data
WHERE content = '调温'
ORDER BY collect_time DESC
