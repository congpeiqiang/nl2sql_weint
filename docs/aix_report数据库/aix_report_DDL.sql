-- Database: aix_report
-- Export date: 2026-07-16 15:48:17
-- Total tables: 10

-- ============================================
-- Table: algorithm_script
-- ============================================
CREATE TABLE `algorithm_script` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `stage_type` int NOT NULL,
  `script` text COLLATE utf8mb4_bin,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- ============================================
-- Table: crystal_analysis_result
-- ============================================
CREATE TABLE `crystal_analysis_result` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `unique_id` varchar(100) COLLATE utf8mb4_bin NOT NULL,
  `stove_no` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL COMMENT '炉台号',
  `is_broken` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT '断线类型',
  `body_length` double DEFAULT NULL COMMENT '等径长度',
  `is_broken_at_crown` tinyint DEFAULT NULL COMMENT '是否放断',
  `survival_status` tinyint DEFAULT NULL COMMENT '是否成活',
  `growth_time` datetime DEFAULT NULL COMMENT '引晶时间',
  `growth_power` double DEFAULT NULL COMMENT '引晶功率',
  `surplus_weight` double DEFAULT NULL COMMENT '剩余料量',
  `growth_temperature1` double DEFAULT NULL COMMENT '引晶开始液面温度',
  `growth_temperature2` double DEFAULT NULL COMMENT '引晶结束液面温度',
  `growth_avg_seed` double DEFAULT NULL COMMENT '引晶平均拉速',
  `growth_duration` double DEFAULT NULL COMMENT '引晶时长 (秒)',
  `crown_duration` double DEFAULT NULL COMMENT '放肩时长 (秒)',
  `broken_length` double DEFAULT NULL COMMENT '断线长度',
  `growth_start_time` datetime DEFAULT NULL,
  `crown_growth_start_time` datetime DEFAULT NULL COMMENT '放肩开始时间',
  `crown_growth_end_time` datetime DEFAULT NULL COMMENT '放肩结束时间',
  `body_start_time` datetime DEFAULT NULL COMMENT '等径开始时间',
  `body_end_time` datetime DEFAULT NULL COMMENT '等径结束时间',
  `recommended_growth_power` double DEFAULT NULL COMMENT '推荐引晶功率',
  `recommended_body_power` double DEFAULT NULL COMMENT '等径模型推荐头部功率',
  `crystal_diameter` double DEFAULT NULL COMMENT '晶体直径',
  `last_power_adjust_diameter` double DEFAULT NULL COMMENT '放肩最后一次功率调整时直径',
  `after_crown_step` varchar(100) COLLATE utf8mb4_bin DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `crystal_analysis_result_UN` (`unique_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='模型效果分析表';

-- ============================================
-- Table: crystal_growth_record
-- ============================================
CREATE TABLE `crystal_growth_record` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `device_code` varchar(20) COLLATE utf8mb4_bin NOT NULL COMMENT '设备编码，炉台号',
  `puller_batch_no` varchar(50) COLLATE utf8mb4_bin NOT NULL COMMENT '炉次编号',
  `ingot_no` int NOT NULL COMMENT '棒次编号, 同一个炉次内唯一：1、2、3...',
  `puller_start_time` datetime DEFAULT NULL COMMENT '开炉时间',
  `stabilize_temperature_start_time` datetime DEFAULT NULL COMMENT '调温开始时间(棒次开始时间)',
  `neck_growth_start_time` datetime DEFAULT NULL COMMENT '引晶开始时间',
  `neck_growth_end_time` datetime DEFAULT NULL COMMENT '引晶结束时间',
  `neck_growth_status` tinyint DEFAULT NULL COMMENT '引晶是否成功：0：失败，1：成功，null：引晶未完成',
  `crown_growth_start_time` datetime DEFAULT NULL COMMENT '放肩开始时间',
  `crown_growth_end_time` datetime DEFAULT NULL COMMENT '放肩结束时间（以放肩结束时间算引放次数）',
  `crown_growth_type` varchar(10) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '放肩类型, 人工：MANUAL/自动：AUTO',
  `crown_growth_status` tinyint DEFAULT NULL COMMENT '放肩是否成功,0：失败，1：成功，null：放肩未完成',
  `shoulder_out_time` datetime DEFAULT NULL COMMENT '取肩时间',
  `shoulder_growth_start_time` datetime DEFAULT NULL COMMENT '转肩开始时间',
  `body_growth_start_time` datetime DEFAULT NULL COMMENT '等径开始时间',
  `body_growth_ingot_out_time` datetime DEFAULT NULL COMMENT '等径取段时间',
  `body_growth_end_time` datetime DEFAULT NULL COMMENT '等径结束时间',
  `tail_growth_start_time` datetime DEFAULT NULL COMMENT '收尾开始时间',
  `tail_growth_end_time` datetime DEFAULT NULL COMMENT '收尾结束时间',
  `current_stage` varchar(40) COLLATE utf8mb4_bin NOT NULL COMMENT '当前工步',
  `completed` tinyint NOT NULL COMMENT '棒次是否结束',
  `manual_time` datetime DEFAULT NULL COMMENT '手动更新时间',
  `ingot_data_start_time` datetime DEFAULT NULL COMMENT '棒次数据开始时间',
  `ingot_data_end_time` datetime DEFAULT NULL COMMENT '棒次数据结束时间',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
  PRIMARY KEY (`id`),
  KEY `crystal_growth_record_device_code_index` (`device_code`),
  KEY `crystal_growth_record_puller_batch_no_index` (`puller_batch_no`)
) ENGINE=InnoDB AUTO_INCREMENT=195477 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='单晶硅生长记录表';

-- ============================================
-- Table: crystal_process_analysis_record
-- ============================================
CREATE TABLE `crystal_process_analysis_record` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `crystal_growth_record_id` bigint NOT NULL,
  `device_code` varchar(20) COLLATE utf8mb4_bin NOT NULL COMMENT '设备编码，炉台号',
  `puller_batch_no` varchar(50) COLLATE utf8mb4_bin NOT NULL COMMENT '炉次编号',
  `puller_start_time` datetime DEFAULT NULL COMMENT '开炉时间',
  `ingot_no` int NOT NULL COMMENT '棒次编号',
  `ingot_start_time` datetime DEFAULT NULL COMMENT '棒次开始时间(调温开始时间)',
  `recharge_times` int DEFAULT NULL COMMENT '复投次数',
  `running_time_seconds` int DEFAULT NULL COMMENT '运行时间(秒)',
  `stabilize_temperature_actual_melt_position` float(8,2) DEFAULT NULL COMMENT '调温实际液口距',
  `stabilize_temperature_intellig_rate` float(8,5) DEFAULT NULL COMMENT '调温智能拉晶控制比率',
  `neck_growth_start_power` float(8,2) DEFAULT NULL COMMENT '引晶开始时主加功率',
  `neck_growth_start_melt_temperature` float(8,2) DEFAULT NULL COMMENT '引晶开始液温',
  `neck_growth_start_remaining_weight` float(8,2) DEFAULT NULL COMMENT '引晶开始时剩余料量',
  `neck_growth_end_melt_temperature` float(8,2) DEFAULT NULL COMMENT '引晶结束液温',
  `neck_growth_pull_rate` float(8,2) DEFAULT NULL COMMENT '引晶平均拉速',
  `neck_growth_pull_rate_end_100mm` float(8,2) DEFAULT NULL COMMENT '引晶后100mm拉速',
  `neck_growth_end_melt_position` float(8,2) DEFAULT NULL COMMENT '引晶完成液口距',
  `neck_growth_end_crystal_length` float(8,2) DEFAULT NULL COMMENT '引晶结束晶体长度',
  `neck_growth_start_liq_lvl_temp_avg` decimal(10,5) DEFAULT NULL COMMENT '引晶开始亮度均值',
  `neck_growth_end_liq_lvl_temp_avg` decimal(10,5) DEFAULT NULL COMMENT '引晶结束亮度均值',
  `neck_growth_rem_weight_avg` float(8,5) DEFAULT NULL COMMENT '引晶结束剩余料量均值',
  `neck_growth_cryst_rot_avg` float(8,5) DEFAULT NULL COMMENT '引晶结束晶转均值',
  `neck_growth_cruc_rot_avg` float(8,5) DEFAULT NULL COMMENT '引晶结束埚转均值',
  `crown_growth_start_cruc_rot` float(8,5) DEFAULT NULL COMMENT '放肩开始晶转',
  `crown_growth_crystal_diameter_5_min_avg` float(8,5) DEFAULT NULL COMMENT '放肩晶体直径均值5分钟',
  `crown_growth_crystal_diameter_30_min_avg` float(8,5) DEFAULT NULL COMMENT '放肩晶体直径均值30分钟',
  `crown_growth_crystal_diameter_60_min_avg` float(8,5) DEFAULT NULL COMMENT '放肩晶体直径均值60分钟',
  `crown_growth_crystal_diameter_90_min_avg` float(8,5) DEFAULT NULL COMMENT '放肩晶体直径均值90分钟',
  `final_state` varchar(50) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '晶体最终状态',
  `crown_growth_start_cryst_rot` float(8,5) DEFAULT NULL COMMENT '放肩开始埚转',
  `is_manual_crown_growth` tinyint DEFAULT NULL COMMENT '放肩是否人工干预 ：1：有人工干预 0：没人工干预 没数据：NULL',
  `manual_crown_growth_details` varchar(500) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '放肩人工干预详情',
  `is_manual_body_growth` tinyint DEFAULT NULL COMMENT '等径是否人工干预 ：1：有人工干预 0：没人工干预\nnull: 没数据',
  `manual_body_growth_details` varchar(500) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '等径是否人工干预详情',
  `crown_growth_3mm_crystal_diameter` float(8,2) DEFAULT NULL COMMENT '放肩3mm时的晶体直径',
  `crown_growth_120mm_crystal_act_diameter` float(8,5) DEFAULT NULL COMMENT '放肩晶长120mm实际直径',
  `crown_growth_crystal_diameter` float(8,2) DEFAULT NULL COMMENT '放肩结束晶体直径',
  `crown_growth_end_crystal_length` float(8,2) DEFAULT NULL COMMENT '放肩结束晶体长度',
  `crown_growth_end_melt_position` float(8,2) DEFAULT NULL COMMENT '放肩完成液口距',
  `crown_growth_power_diff_30` float(8,2) DEFAULT NULL COMMENT '放肩30min时主加功率最大差值',
  `crown_growth_power_diff_60` float(8,2) DEFAULT NULL COMMENT '放肩60min时主加功率最大差值',
  `crown_growth_intellig_rate` float(8,5) DEFAULT NULL COMMENT '放肩智能拉晶控制比率',
  `shoulder_growth_start_power` float(8,2) DEFAULT NULL COMMENT '转肩开始时主加功率',
  `shoulder_growth_end_melt_position` float(8,2) DEFAULT NULL COMMENT '转肩完成液口距',
  `shoulder_growth_start_act_diameter` float(8,5) DEFAULT NULL COMMENT '转肩开始实际直径',
  `body_growth_start_power` float(8,2) DEFAULT NULL COMMENT '等径起始功率',
  `body_growth_start_set_diameter` float(8,5) DEFAULT NULL COMMENT '等径开始设定直径',
  `body_growth_complete_melt_position_crystal_length` float(8,2) DEFAULT NULL COMMENT '等径液口距补齐晶体长度',
  `body_growth_complete_melt_position` float(8,2) DEFAULT NULL COMMENT '等径补齐液口距',
  `body_growth_lowest_power` float(8,2) DEFAULT NULL COMMENT '等径最低功率',
  `body_growth_50mm_pull_rate` varchar(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '等径50mm拉速（[49.0 - 51.0]之间的数据）',
  `body_growth_100mm_pull_rate` varchar(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '等径100mm拉速（[99.0 - 101.0]之间的数据）',
  `body_growth_150mm_pull_rate` varchar(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '等径150mm拉速（[149.0 - 151.0]之间的数据）',
  `body_growth_200mm_pull_rate` varchar(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '等径200mm拉速（[199.0 - 201.0]之间的数据）',
  `body_growth_end_crystal_length` float(8,2) DEFAULT NULL COMMENT '等径结束晶体长度',
  `body_growth_end_net_weight` float(8,2) DEFAULT NULL COMMENT '等径结束晶体净重',
  `body_growth_end_melt_position` float(8,2) DEFAULT NULL COMMENT '等径结束设定液口距',
  `body_growth_intellig_rate` float(8,5) DEFAULT NULL COMMENT '等径智能拉晶控制比率',
  `body_growth_target_power` float(8,5) DEFAULT NULL COMMENT '等径目标功率',
  `body_growth_target_power_range_of_crystal_length` varchar(100) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '等径目标功率对应晶长范围',
  `body_growth100mm_pull_rate_difference` float(8,5) DEFAULT NULL COMMENT '等径目标功率100mm',
  `body_growth100mm_pull_rate_avg` float(8,5) DEFAULT NULL COMMENT '100mm拉速差均值',
  `body_growth200mm_pull_rate_difference` float(8,5) DEFAULT NULL COMMENT '等径拉速差200mm',
  `body_growth200mm_pull_rate_avg` float(8,5) DEFAULT NULL COMMENT '200mm拉速差均值',
  `body_growth300mm_pull_rate_difference` float(8,5) DEFAULT NULL COMMENT '等径拉速差300mm',
  `body_growth300mm_pull_rate_avg` float(8,5) DEFAULT NULL COMMENT '300mm拉速差均值',
  `body_growth400mm_pull_rate_difference` float(8,5) DEFAULT NULL COMMENT '等径拉速差400mm',
  `body_growth400mm_pull_rate_avg` float(8,5) DEFAULT NULL COMMENT '400mm拉速差均值',
  `body_growth500mm_pull_rate_difference` float(8,5) DEFAULT NULL COMMENT '等径拉速差500mm',
  `body_growth500mm_pull_rate_avg` float(8,5) DEFAULT NULL COMMENT '500mm拉速差均值',
  `ingot_out_start_main_water_dist_temp` float(8,2) DEFAULT NULL COMMENT '取段开始主分水器水温',
  `ingot_out_start_heat_shield_water_temp` float(8,2) DEFAULT NULL COMMENT '热屏水温',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录更新时间',
  `body_growth_act_main_pow_50mm` float(8,5) DEFAULT NULL COMMENT '50mm实际主加功率',
  `attribute` text COLLATE utf8mb4_bin,
  PRIMARY KEY (`id`),
  UNIQUE KEY `crystal_process_analysis_record_uk` (`crystal_growth_record_id`),
  KEY `crystal_process_analysis_record_device_code_index` (`device_code`),
  KEY `crystal_process_analysis_record_puller_batch_no_index` (`puller_batch_no`)
) ENGINE=InnoDB AUTO_INCREMENT=14 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='单晶硅生长工艺分析表';

-- ============================================
-- Table: device_info
-- ============================================
CREATE TABLE `device_info` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `stove_no` varchar(100) COLLATE utf8mb4_bin NOT NULL COMMENT '炉台号',
  `area` varchar(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '区域',
  `workshop` varchar(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '车间',
  `manufacturer` varchar(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '设备厂家',
  `enabled` bit(1) DEFAULT NULL COMMENT '启用',
  `latest_update_time` datetime DEFAULT NULL COMMENT '最后更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `device_info_UN` (`stove_no`)
) ENGINE=InnoDB AUTO_INCREMENT=12 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='设备信息表';

-- ============================================
-- Table: flyway_schema_history
-- ============================================
CREATE TABLE `flyway_schema_history` (
  `installed_rank` int NOT NULL,
  `version` varchar(50) COLLATE utf8mb4_bin DEFAULT NULL,
  `description` varchar(200) COLLATE utf8mb4_bin NOT NULL,
  `type` varchar(20) COLLATE utf8mb4_bin NOT NULL,
  `script` varchar(1000) COLLATE utf8mb4_bin NOT NULL,
  `checksum` int DEFAULT NULL,
  `installed_by` varchar(100) COLLATE utf8mb4_bin NOT NULL,
  `installed_on` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `execution_time` int NOT NULL,
  `success` tinyint(1) NOT NULL,
  PRIMARY KEY (`installed_rank`),
  KEY `flyway_schema_history_s_idx` (`success`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- ============================================
-- Table: growth_result
-- ============================================
CREATE TABLE `growth_result` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `unique_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL COMMENT '依据炉号与引晶时间为依据生成',
  `stove_no` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT '炉号',
  `crucible` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT '坩埚',
  `is_broken` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT '是否断线',
  `growth_power` double DEFAULT NULL COMMENT '引晶功率',
  `model_power` double DEFAULT NULL COMMENT '模型功率',
  `growth_time` datetime DEFAULT NULL COMMENT '引晶时间',
  `surplus_weight` double DEFAULT NULL COMMENT '剩余料量',
  `broken_length` double DEFAULT NULL COMMENT '断线长度',
  `max_liquid_temp` double DEFAULT NULL COMMENT '控温最高液温',
  `start_stable_temp_date_time` datetime DEFAULT NULL COMMENT '开始稳温的时间',
  `stable_temp_duration` double DEFAULT NULL COMMENT '稳温时长',
  `growth_temperature1` double DEFAULT NULL COMMENT '引晶开始液面温度',
  `growth_temperature2` double DEFAULT NULL COMMENT '引晶结束液面温度',
  `temp_drop` double DEFAULT NULL COMMENT '液温下滑量',
  `hundred_pull_rate` double DEFAULT NULL COMMENT '百长拉速',
  `growth_avg_seed` double DEFAULT NULL COMMENT '引晶平均拉速',
  `pull_rate_deviation` double DEFAULT NULL COMMENT '拉速偏差',
  `crown_growth_duration` double DEFAULT NULL COMMENT '放肩时长(秒)',
  `crown_growth_start_time` datetime DEFAULT NULL COMMENT '放肩开始时间',
  `crown_growth_end_time` datetime DEFAULT NULL COMMENT '放肩结束时间',
  `body_growth_start_time` datetime DEFAULT NULL COMMENT '等径开始时间',
  `body_growth_end_time` datetime DEFAULT NULL COMMENT '等径结束时间',
  `manual_intervention` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT '放肩人工干预',
  `body_intervention` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT '等径人工干预',
  `remarks` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT '备注',
  `run_time_seconds` bigint DEFAULT NULL COMMENT '运行时间长（秒）',
  `body_growth_diam` double DEFAULT NULL COMMENT '直径',
  `latest_update_time` datetime DEFAULT NULL COMMENT '最后更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `growth_result_UN` (`unique_id`)
) ENGINE=InnoDB AUTO_INCREMENT=205 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='生长记录';

-- ============================================
-- Table: jinko_mes_data
-- ============================================
CREATE TABLE `jinko_mes_data` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `device_code` varchar(20) COLLATE utf8mb4_bin NOT NULL COMMENT '设备编码，炉台号',
  `puller_batch_no` varchar(50) COLLATE utf8mb4_bin NOT NULL COMMENT '炉次编号',
  `work_order_no` varchar(50) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '工单号',
  `puller_start_time` datetime DEFAULT NULL COMMENT '开炉时间',
  `puller_start_info` text COLLATE utf8mb4_bin COMMENT '开炉信息',
  `puller_start_info_exec_times` int NOT NULL DEFAULT '0' COMMENT '开炉信息执行次数',
  `puller_start_info_status` varchar(20) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '开炉信息状态, 成功：success, 失败：具体信息',
  `material_info` text COLLATE utf8mb4_bin COMMENT '物料信息',
  `material_info_exec_times` int NOT NULL DEFAULT '0' COMMENT '物料信息执行次数',
  `material_info_status` varchar(20) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '物料信息状态, 成功：success, 失败：具体信息',
  `crystal_info` text COLLATE utf8mb4_bin COMMENT '单晶信息',
  `crystal_info_exec_times` int NOT NULL DEFAULT '0' COMMENT '单晶信息执行次数',
  `crystal_info_status` varchar(20) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '单晶信息状态, 成功：success, 失败：具体信息',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `puller_batch_no` (`puller_batch_no`),
  KEY `idx_jinko_mes_data_updated_at` (`updated_at`)
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='晶科MES数据表';

-- ============================================
-- Table: puller_batch_record
-- ============================================
CREATE TABLE `puller_batch_record` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `device_code` varchar(20) COLLATE utf8mb4_bin NOT NULL COMMENT '设备编码，炉台号',
  `puller_batch_no` varchar(50) COLLATE utf8mb4_bin NOT NULL COMMENT '炉次编号',
  `puller_start_time` datetime DEFAULT NULL COMMENT '合炉时间',
  `puller_stop_time` datetime DEFAULT NULL COMMENT '停炉取晶时间',
  `new_puller_batch` tinyint(1) NOT NULL COMMENT '是否新炉次，0：否，1：是',
  `status` varchar(40) COLLATE utf8mb4_bin NOT NULL COMMENT '炉次状态：开炉，运行中，停炉',
  `puller_data_start_time` datetime DEFAULT NULL COMMENT '炉次数据开始时间',
  `puller_data_end_time` datetime DEFAULT NULL COMMENT '炉次数据结束时间',
  `created_stage` varchar(40) COLLATE utf8mb4_bin NOT NULL COMMENT '炉次创建工步',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录更新时间',
  PRIMARY KEY (`id`),
  KEY `puller_batch_record_device_code_index` (`device_code`),
  KEY `puller_batch_record_puller_batch_no_index` (`puller_batch_no`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='炉次记录表';

-- ============================================
-- Table: task
-- ============================================
CREATE TABLE `task` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键',
  `name` varchar(200) COLLATE utf8mb4_bin NOT NULL COMMENT '名称',
  `type` varchar(50) COLLATE utf8mb4_bin NOT NULL COMMENT '任务类型',
  `status` varchar(20) COLLATE utf8mb4_bin NOT NULL COMMENT '任务状态',
  `exec_times` int NOT NULL COMMENT '执行状态',
  `properties` varchar(200) COLLATE utf8mb4_bin DEFAULT NULL,
  `execute_schedule` varchar(200) COLLATE utf8mb4_bin NOT NULL COMMENT '任务执行计划',
  `cost_seconds` bigint NOT NULL DEFAULT '0' COMMENT '任务执行耗时',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=116745 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='设备任务表';
