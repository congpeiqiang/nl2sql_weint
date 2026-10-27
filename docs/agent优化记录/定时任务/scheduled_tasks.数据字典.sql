-- ============================================================================
-- 定时任务库（scheduled_tasks.sqlite）中文列名视图 + 数据字典
--
-- 为什么需要这个文件：SQLite 没有列注释（不支持 COMMENT ON COLUMN），
-- sqlite_master 里只有建表语句原文，所以 DBeaver / DB Browser for SQLite
-- 这类图形工具只能显示 id / owner / next_run_at 这样的英文字段名。
--
-- 做法：建两个视图，列名直接用中文含义。图形工具里展开 Views 点开，
--       表头就是人能读的东西；表与数据本身完全不动。
--
-- 用法：图形工具的「执行 SQL」页把对应区块整体跑一遍（纯新增视图，不改表、
--       不改数据、不动索引）。跑完在对象树里按 F5 刷新。
--
-- ⚠️ 生产库也能跑（后端代码不读写这些视图），介意就在库的副本上跑。
-- ============================================================================


-- ============================================================================
-- 区块 A：当前线上库有的列（发版前后都能跑）
-- ============================================================================

DROP VIEW IF EXISTS v_定时任务;
CREATE VIEW v_定时任务 AS
SELECT
    id                     AS 任务ID,
    owner                  AS 所属用户ID,
    name                   AS 任务名,
    prompt                 AS 问数内容,
    db_name                AS 目标库,
    llm_route              AS 模型路由,
    llm_model              AS 模型名,
    sched_kind             AS 调度类型,
    sched_expr             AS 调度表达式,
    sched_display          AS 调度说明,
    tz                     AS 时区,
    next_run_at            AS 下次触发UTC,
    enabled                AS 已启用,
    state                  AS 状态,
    repeat_times           AS 重复次数,
    repeat_completed       AS 已执行次数,
    running_thread_id      AS 在飞会话ID,
    retry_count            AS 重试计数,
    last_run_at            AS 最近执行UTC,
    last_status            AS 最近状态,
    last_error             AS 最近错误,
    origin_thread_id       AS 来源会话ID,
    created_at             AS 创建UTC,
    updated_at             AS 更新UTC
FROM scheduled_tasks;

DROP VIEW IF EXISTS v_运行流水;
CREATE VIEW v_运行流水 AS
SELECT
    id          AS 流水ID,
    task_id     AS 任务ID,
    thread_id   AS 会话ID,
    run_id      AS 运行ID,
    fired_at    AS 触发UTC,
    finished_at AS 结束UTC,
    status      AS 状态,
    error       AS 错误
FROM task_runs;


-- ============================================================================
-- 区块 B：固化查询（pin_* / pin_mode / pin_reason）
--   这 6+2 个列是 2026-10-23 才随「固化查询」特性加到 dev 分支的（commit 8873093），
--   发版前线上库没有这些列 —— 在旧库上跑本区块会报
--   "no such column: pin_status"，属预期，等后端发过一次再跑。
-- ============================================================================

DROP VIEW IF EXISTS v_定时任务_固化;
CREATE VIEW v_定时任务_固化 AS
SELECT
    id                     AS 任务ID,
    name                   AS 任务名,
    pin_status             AS 固化状态,
    pin_spec               AS 固化模板JSON,
    pin_semantic_version   AS 固化时的语义库版本,
    pin_saved_at           AS 固化时间UTC,
    pin_invalid_reason     AS 固化失效原因,
    pin_source_thread_id   AS 固化来源会话ID
FROM scheduled_tasks;

DROP VIEW IF EXISTS v_运行流水_固化;
CREATE VIEW v_运行流水_固化 AS
SELECT
    id          AS 流水ID,
    task_id     AS 任务ID,
    fired_at    AS 触发UTC,
    status      AS 状态,
    pin_mode    AS 固化模式,
    pin_reason  AS 固化回退原因
FROM task_runs;

-- 固化相关字段取值：
--   pin_status : ''      未固化
--                active   固化有效，触发时走 replay（跳过模型，按日期渲染模板直接执行）
--                invalid  固化失效，触发时退回正常问数，原因见 pin_invalid_reason
--   pin_mode   : normal   正常问数
--                replay   走固化模板
--                fallback 本该走固化但失效了，退回正常问数
--   pin_invalid_reason 取值：semantic_version_changed / spec_version_unknown /
--                render_failed / cube_compile_failed / not_semantic_db / sql_failed /
--                exec_timeout / columns_changed / db_changed / ambiguous_date /
--                unmatched_date


-- ============================================================================
-- 区块 C：字段取值速查（view 里看不出枚举含义时查这里）
-- ============================================================================

-- scheduled_tasks.state
--   scheduled  在调度中（enabled=1 且 next_run_at 到期就会触发）
--   paused     已暂停（同时 enabled=0）
--   completed  完成（once 已跑 / repeat_times 用尽）；enabled 不变，靠 state 拦
--   error      调度表达式已不可解析，停摆等人处理
--
-- scheduled_tasks.enabled
--   1 参与调度   0 不参与。暂停置 0、恢复置 1；completed 时不改这一列
--   （due 选取条件 = enabled=1 AND state='scheduled'，两个条件都要满足）
--
-- scheduled_tasks.sched_kind
--   cron     sched_expr = 5 字段 cron 表达式
--   interval sched_expr = 间隔分钟数
--   once     sched_expr = 一次性触发的 ISO 时间点
--
-- scheduled_tasks.prompt
--   自包含问数内容，落地成「[定时任务] {name}：{prompt}（这是定时自动执行…）」发给 agent。
--
-- scheduled_tasks.owner
--   user_id。服务端触发时以此为准，不信任任何请求体（越权防线）。
--
-- scheduled_tasks.running_thread_id
--   非空 = 这一轮还在飞。长期非空且 last_run_at 很旧 ⇒ 卡死，等 reconcile/看门狗收尾。
--
-- 时间口径：next_run_at / fired_at / created_at 等一律 UTC ISO（T 口径，带 Z 或 +00:00），
-- 不是北京时间。界面上显示的是本地时区，两者差 8 小时是正常的。
--
-- task_runs.status
--   running    已派发，还没收到 run 终态（默认值）
--   success    成功
--   error      出错
--   timeout    超时
--   interrupted 被打断（看门狗/服务重启）
--   cancelled  用户主动停止
--   取值来自 worker.TERMINAL_STATUSES；非这几种的、长期 running 的 = 等 reconcile 收尾。
--
-- 保留策略：task_runs 只保留 90 天（worker 每 1 小时至多清一次）；
-- scheduled_tasks 不自动清理，删任务时连带删它的流水。
