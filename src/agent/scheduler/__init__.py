"""定时任务子系统（cron / interval / once）。

三件套：
- ``store``    SQLite 持久层（一库一目录 + 共享连接一把锁，读也进锁）
- ``service``  校验与 CRUD（API 与聊天工具的**唯一**实现，"NO second job engine"）
- ``worker``   lifespan daemon 线程：60s tick、grace 折叠、CAS 认领、fire、reconcile

设计骨架来自 hermes-agent cron 子系统（详见 docs/NL2SQL定时任务实现方案.md）：
``next_run_at`` 持久化、执行前先 CAS 推进（at-most-once）、在飞跳过（不重入）、
misfire 超 grace 折叠积压但仍立即跑一次。
"""
