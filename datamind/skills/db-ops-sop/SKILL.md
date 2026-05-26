---
name: db-ops-sop
description: Database operations runbook — backup, recovery, performance tuning, troubleshooting. Use for SQLite / MySQL / PostgreSQL ops questions, slow-query diagnosis, or outage response.
keywords: [database, 数据库, backup, restore, SOP, MySQL, PostgreSQL, SQLite, 备份, 恢复, 运维]
---

# 数据库运维 SOP

## 适用场景

当用户咨询数据库备份、恢复、性能优化、故障排查等运维相关问题时，参考本指南进行回答。

## 一、数据库备份

### 1.1 全量备份

```bash
# SQLite 备份（直接复制文件）
cp /path/to/database.db /path/to/backup/database_$(date +%Y%m%d).db

# MySQL 全量备份
mysqldump -u root -p --all-databases > full_backup_$(date +%Y%m%d).sql

# PostgreSQL 全量备份
pg_dump -U postgres -F c dbname > backup_$(date +%Y%m%d).dump
```

### 1.2 增量备份

- MySQL: 使用 binlog 进行增量备份
- PostgreSQL: 使用 WAL 归档进行增量备份
- 建议每日全量 + 每小时增量

### 1.3 备份策略建议

| 频率 | 类型 | 保留时间 |
|------|------|---------|
| 每日 | 全量备份 | 30 天 |
| 每小时 | 增量备份 | 7 天 |
| 每周 | 异地备份 | 90 天 |

## 二、数据库恢复

### 2.1 恢复步骤

1. 停止应用服务
2. 确认备份文件完整性
3. 执行恢复命令
4. 验证数据一致性
5. 重启应用服务

### 2.2 恢复命令

```bash
# SQLite 恢复
cp /path/to/backup/database_20240101.db /path/to/database.db

# MySQL 恢复
mysql -u root -p < full_backup_20240101.sql

# PostgreSQL 恢复
pg_restore -U postgres -d dbname backup_20240101.dump
```

## 三、性能优化

### 3.1 慢查询排查

1. 开启慢查询日志
2. 分析 TOP 10 慢查询
3. 使用 EXPLAIN 分析执行计划
4. 添加必要索引
5. 优化 SQL 语句

### 3.2 常见优化手段

- **索引优化**: 为高频查询字段添加索引，避免全表扫描
- **查询优化**: 避免 SELECT *，减少子查询，使用 JOIN 替代
- **连接池**: 使用连接池管理数据库连接，避免频繁创建/销毁
- **读写分离**: 大流量场景下主库写、从库读
- **缓存**: 热点数据使用 Redis 缓存

## 四、故障排查清单

1. 检查数据库服务是否运行
2. 检查磁盘空间是否充足
3. 检查连接数是否达到上限
4. 检查慢查询是否阻塞
5. 检查主从同步状态
6. 检查锁等待情况
