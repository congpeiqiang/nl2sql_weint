# 1.涉及的应用场景

`下面是跟开发人员了解数据库表后，划分的场景，用于下一步编写对应的cubes聚合指标`

## 1.1 工时 聚合初版

### 1.1.1 统计维度

`从下面几个维度统计投入的工时`

- 项目
- 人员
- 时间区间
- 审核通过否
- 未填/漏填/闲置
- **待补充还有哪些**

```
do_work_hour 每日报工的工时
do_work_hour_detail(工时项目)  不太清楚
do_work_hour_detail_extend（工时明细表）
		工时详细信息
do_work_hour_examine（工时审核）
_user_detail（工时投入明细表） 废弃
do_work_order（工单表）
		工单信息
do_work_overtime（加班） 废弃
```

## 1.2 bug缺陷 聚合初版

`从下面几个维度统计`

- 项目
- 提交人
- 时间区间
- **待补充还有哪些**

```
do_bug(bug缺陷)
	product_line字段 作废
	project_collect字段 作废
	industry_dic_config_code字段 作废  

do_bug_project（缺陷应用项目,哪个项目上的bug）移除
do_bug_wide（bug宽表）移除
```

## 1.3 任务 聚合初版

```
do_task (任务详情)
do_task_log  开发人员自用
do_task_report（任务属性）
do_task_scheduler_history（定时任务运行历史表）   废弃
do_task_scheduler_model（定时任务模型表）   废弃
do_task_scheduler_run（定时任务运行表）   废弃
do_task_score（任务评价）   废弃
do_task_tag    废弃
```

## 1.4 需求 聚合初版

```
do_story
		需求主表：交付/研发需求的完整信息（状态、类型、优先级、计划与实际时间、负责人、所属项目/模块/迭代）。状态 14=已完成、6=已取消、99=关闭；统计完成率须排除已取消（status<>6）
do_story_project（需求应用项目） 需求和项目映射
do_story_rel（依赖项）

do_story_wide（需求宽表） 移除
```

## 1.5 部门

```
do_department(部门,公司部门和组织架构相关信息,优先从该表查询)
do_custom_department(部门，管理员自己创建的组织架构)
do_department_user(部门员工,部门包含的员工)
do_department_user_detail(部门员工详情)

do_department_project(部门项目) 移除
```

## 1.6 项目 聚合初版

```
do_project 
do_project_module(项目模块，记录项目负责人)
do_project_product （项目关联产品）
do_project_product_module（项目产品模块关联，项目和模块通过产品关联） 
do_project_role（项目角色）项目包含角色
do_project_role_menu（项目角色菜单）项目中，不同角色可以看到的菜单项
do_project_user_config(项目人员配置) 项目关联员工 员工部门
do_project_user_role(项目人员角色)
		project_user_id？未知
		project_role_id？未知
		
do_project_review（项目评审）assigned_to_user_id 字段不使用  移除
do_project_user 项目关联员工  移除
do_license_manager（项目标签关联表）  废弃
do_license_meta（认证数据表）  废弃
do_project_group（项目分组配置）  废弃
do_project_initiation（项目立项）  废弃
do_project_menu（项目菜单）  废弃
do_project_module_flow  废弃
do_project_service（部署服务）  废弃
do_project_tag（项目标签关联表）  废弃
```

## 1.7 迭代 聚合初版

```
do_iteration（迭代）
do_iteration_burndown（迭代燃尽表）
do_iteration_detail（转迭代记录）
do_iteration_project（迭代项目关联表）
```

## 1.8 请假 -移除

```
do_leave_form（请假条）
do_attendance_confirm(请假条) 移除
do_attendance_correction(考勤修正) 移除
```

## 1.9 VPN -移除

```
do_vpn_apply（vpn申请）
do_vpn_apply_detail（vpn申请明细）
```

## 1.20 出差 -移除

```
do_evection
do_evection_together_user
```

## 1.21 机票预定 -移除

```
do_flight_reservation（机票预定）  
do_flight_reservation_detail(机票预定明细)  
do_flight_reservation_user_detail(住宿流程)
```

## 1.22 流程操作  暂不聚合

```
do_flow_handle_record(流程操作记录) 表中flow_instance、flow_node_task、flow_node这三个字段在其他库
do_flow_task_user (关联人员)
```

## 1.23 积分商品兑换 -移除 

```
do_good_exchange(积分商品兑换台账)
do_point_good（积分商品台账）
```

## 1.24  指标 -移除

```
do_indicator(指标库)
do_indicator_group(指标库)
do_indicator_instance(指标实例)
do_indicator_value(指标值)
do_indicator_value_instance(指标值实例)
```

## 1.27 采购 -移除

```
do_purchase_contract（采购合同）
```

## 1.28 公司物品、物料维度 -移除

```
do_material（物料）
do_material_outward（资料外发申请）
do_material_repair（物品维修）
do_material_requisition（物料领用）
do_material_requisition_detail（物料领用明细）
do_material_requisition_person（物品领用人员）
```

## 1.29 调查问卷 -移除 

```
do_question
do_questionnaire（问卷）
do_questionnaire_indicator（问卷指标）
do_questionnaire_indicator_instance（问卷指标实例）
do_questionnaire_instance（问卷实例）
```

## 1.30 合理化建议 -移除

```
do_rationalization_proposal（合理化建议）
do_rationalization_proposal_project
```

## 1.31 报销 -移除

```
do_reimbursement（报销）
do_reimbursement_detail（报销明细）
```

## 1.32 发版 -移除

```
do_release（发版）
do_release_task（功能清单）
do_service_version(部署版本)
```

## 1.33 积分 -移除

```
do_score
do_score_log（积分操作记录）
```

## 1.34 需求、工单流转

```
do_carbon_copy_user 需求抄送表
do_move_log 需求、工单等流转日志，例如修改哪些字段
```

## 1.35 报表 暂不聚合

```
do_tagdo_table（报表）
do_table_head（报表表头）
do_table_link（表头关联）
do_table_month（报表月份表）
do_table_user（报表花名册表）
```

## 1.36 user 

```
do_user_link（用户关联信息）

do_use_case   移除
do_user_config 移除
do_user_group 移除
do_user_group_project_module 移除
```

## 1.37 需求、缺陷记录

```
do_view_record（变更查看记录表）
```

## 1.38 节假日 工作日

```
do_date_config (日期配置,工作日和节假日)
```

## 剩余表

```
do_annex(附件表) 保留
do_comment(评论) 保留
do_comment_history(评论) 保留
do_contract_renewal(合同续签) 移除
do_batch(git分支管理)   移除
do_batch_story(git分支管理)   移除
do_branch(git分支管理)   移除
do_code_review(codeReview基础详情) 移除
do_code_review_column(codeReview字段)  移除
do_depart(离职,员工离职信息)  移除
do_dashboard(看板)  移除
do_dashboard_column(看板)  移除
do_dashboard_column_task(看板)  移除
do_business_user（关联人员） 移除
do_evaluation_activity(360评估) 移除
do_evaluation_activity(评估活动问卷配置)  移除
do_evaluation_activity_user_config(评估人员配置)  移除
do_evaluation_activity_user_score(评估人员分数)  移除
do_evaluation_activity_user_info(用户信息)  移除
do_evaluation_indicator_user_annex(评估人员分数附件) 移除
do_evaluation_self(评估人员配置) 移除
do_evaluation_self_annex(评估人员配置) 移除
do_evaluation_self_check(自评) 移除
do_evaluation_activity_questionnaire(评估活动问卷配置) 移除
do_exception_handling（紧急异常处理）   移除
do_external_communication(外部沟通管理权限)    移除
do_external_communication(外部沟通管理权限)    移除
do_materialdo_manager_percent（管理层投入占比表）移除
do_record_daily   移除
do_service_versiondo_seal(用品印章)     移除
do_message_confirm表？不太清楚该表
do_module_config？   移除
do_node_number?do_node_status?do_node_task?do_node_work_time? 移除
do_payment？   移除
do_person_transfer？   移除
do_problem？不太清楚该表
do_sonar_issue_bug_ref(部门)?   移除
do_stage?   移除
do_stay(住宿流程)?是否不使用了？   移除
do_store_house？   移除
do_system_config？   移除
do_tabledo_system_service？   移除
do_taskdo_tag？ 移除
do_team_person_transfer（部门项目）？   移除
do_traffic_trend？    移除
flow_instance_element_user？ 移除
flyway_schema_history？ 移除
```

# 2. 语义库过滤物理表和字段

- 整表不需要对外查询

  解决方案:

    1.不需要的表，不在 MDL 中定义,实现整表屏蔽｜

    2.兜底防护：NL2SQL 生成 SQL 之后，AST 黑名单校验（`sqlglot`推荐）

- 单表内部分字段不开放

  解决方案：

    1.同一张表，MDL中部分字段使用 `hidden = true` 标记；

     `注解:`

  ​        `   1.hidden 字段**不会注入 LLM 上下文**：大模型看不到这个字段，无法在 where/group by/select 直接使用；`   

  ​       ` 2.可以在 measure（度量）表达式内引用 hidden 字段做聚合计算**（核心特性！）`   

     2.兜底防护：NL2SQL 生成 SQL 之后，AST 黑名单校验（`sqlglot`推荐）



# 3. 其余

## 3.1 所有带dic_config_code 字典项要在sys_mng的数据库中的字典表