-- Ad Helper 千川代理商 ERP 建表脚本（MySQL 8+ / utf8mb4）
CREATE DATABASE IF NOT EXISTS ad_helper DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE ad_helper;

CREATE TABLE IF NOT EXISTS oauth_token (
  id INT NOT NULL AUTO_INCREMENT COMMENT '主键',
  agent_id BIGINT NULL COMMENT '工作台/代理商管理账户ID',
  agent_name VARCHAR(255) NULL COMMENT '管理账户名称',
  account_role VARCHAR(64) NULL COMMENT '授权账户角色',
  access_token TEXT NOT NULL COMMENT '访问令牌',
  refresh_token TEXT NOT NULL COMMENT '刷新令牌',
  expires_at DATETIME NOT NULL COMMENT 'access_token 过期时间',
  refresh_expires_at DATETIME NOT NULL COMMENT 'refresh_token 过期时间',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  KEY idx_oauth_token_agent_id (agent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代理商OAuth令牌';

CREATE TABLE IF NOT EXISTS qianchuan_advertiser (
  id INT NOT NULL AUTO_INCREMENT COMMENT '主键',
  agent_id BIGINT NOT NULL COMMENT '所属代理商ID',
  advertiser_id BIGINT NOT NULL COMMENT '千川子账户ID',
  advertiser_name VARCHAR(255) NOT NULL DEFAULT '' COMMENT '账户名称',
  status VARCHAR(64) NOT NULL DEFAULT 'UNKNOWN' COMMENT '账户状态',
  sync_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最近同步时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_advertiser_id (advertiser_id),
  KEY idx_qianchuan_advertiser_agent_id (agent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代理商下属千川子账户';

CREATE TABLE IF NOT EXISTS qianchuan_ad_plan (
  id INT NOT NULL AUTO_INCREMENT COMMENT '主键',
  advertiser_id BIGINT NOT NULL COMMENT '千川子账户ID',
  ad_id BIGINT NOT NULL COMMENT '计划ID',
  ad_name VARCHAR(512) NOT NULL DEFAULT '' COMMENT '计划名称',
  roi2_goal DECIMAL(10,4) NULL COMMENT '支付ROI2目标',
  plan_status VARCHAR(64) NOT NULL DEFAULT '' COMMENT '计划状态',
  sync_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最近同步时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_adv_ad (advertiser_id, ad_id),
  KEY idx_qianchuan_ad_plan_advertiser_id (advertiser_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='千川计划基础信息';

CREATE TABLE IF NOT EXISTS qianchuan_ad_report (
  id INT NOT NULL AUTO_INCREMENT COMMENT '主键',
  advertiser_id BIGINT NOT NULL COMMENT '千川子账户ID',
  ad_id BIGINT NOT NULL COMMENT '计划ID',
  stat_date DATE NOT NULL COMMENT '统计日期',
  cost DECIMAL(16,4) NOT NULL DEFAULT 0 COMMENT '消耗',
  pay_order_count INT NOT NULL DEFAULT 0 COMMENT '成交订单数',
  pay_amount DECIMAL(16,4) NOT NULL DEFAULT 0 COMMENT '实付/成交金额',
  roi DECIMAL(16,4) NULL COMMENT 'ROI',
  sync_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最近同步时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_adv_ad_date (advertiser_id, ad_id, stat_date),
  KEY idx_qianchuan_ad_report_advertiser_id (advertiser_id),
  KEY idx_qianchuan_ad_report_ad_id (ad_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='千川计划按日报表';
