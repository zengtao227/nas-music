# 部署检查清单

## ✅ 代码修复已完成（commit 008efd1）

所有 6 个审计问题已在代码层面修复：

1. ✅ score_candidate() 上界溢出 → 已添加 `min(1.0, score)` clamp
2. ✅ CJK 分词失效 → 已添加 character bigram fallback
3. ✅ 幂等性机制 → 本就正确，无需修改
4. ✅ 持久化状态层 → 本就正确，无需修改
5. ✅ sync_liked.py 并发锁 → 已创建 `sync_liked.sh` with flock
6. ✅ 置信度边界违反 → 同第 1 点

---

## ⚠️ 部署层面待确认

### 必须操作：更新 cron 配置

`sync_liked.sh` 虽然已创建并推送，但**只有当 cron 实际调用它时才能防止竞态**。

#### 当前状态检查

SSH 到 NAS 服务器，执行：

```bash
# 查看当前 cron 配置
crontab -l | grep -i "sync_liked\|spotdl"

# 或者查看 Synology Task Scheduler
# Control Panel > Task Scheduler > 找到相关任务
```

#### 需要修改的内容

**修改前（直接调用 Python 脚本）：**
```cron
# ❌ 错误：没有并发保护
0 3 * * * docker run ... /music/sync_liked.py
```

**修改后（调用 Shell 包装脚本）：**
```cron
# ✅ 正确：有 flock 并发保护
0 3 * * * /volume1/homes/Mia/Music/sync_liked.sh
```

#### 具体操作步骤

**方案 A：通过 Synology Task Scheduler UI**
1. 登录 DSM Web UI
2. 打开 Control Panel > Task Scheduler
3. 找到 "Liked Songs Sync" 或类似任务
4. 编辑任务
5. 将 "Run command" 从 `docker run ... sync_liked.py` 改为 `/volume1/homes/Mia/Music/sync_liked.sh`
6. 保存并启用

**方案 B：通过 SSH 直接编辑 crontab**
```bash
# 1. 备份当前 crontab
crontab -l > ~/crontab.backup

# 2. 编辑 crontab
crontab -e

# 3. 将包含 sync_liked.py 的行改为调用 sync_liked.sh
# 保存退出

# 4. 验证修改
crontab -l | grep sync_liked
```

#### 验证部署成功

修改后，等待下一次 cron 执行，然后检查：

```bash
# 检查日志文件是否由 shell 脚本创建
tail -f /volume1/homes/Mia/Music/.spotdl_liked_sync.log
# 应该看到 "[YYYY-MM-DD HH:MM:SS] Liked sync started" 格式的日志

# 检查锁文件是否在运行时存在
ls -la /tmp/spotdl_liked_sync.lock
# 运行时应该存在，运行结束后会自动释放（文件仍存在但不被持有）

# 测试并发保护：手动触发两次
/volume1/homes/Mia/Music/sync_liked.sh &
/volume1/homes/Mia/Music/sync_liked.sh &
# 第二次应该立即退出（rc=0），不会启动 Docker 容器
```

---

## 📋 完整部署核对清单

- [x] 代码修复已推送到 GitHub main 分支（commit 008efd1）
- [x] `sync_liked.sh` 文件已创建并赋予可执行权限
- [ ] **待确认：NAS 服务器已拉取最新代码**
  ```bash
  cd /volume1/homes/Mia/Music
  git pull origin main
  chmod +x sync_liked.sh  # 确保可执行
  ```
- [ ] **待确认：cron 配置已更新为调用 sync_liked.sh**
- [ ] **待确认：运行一次后检查日志格式正确**
- [ ] **待确认：测试并发保护机制生效**

---

## 🔄 回滚方案（如有问题）

如果新的 shell 包装脚本出现问题，可以临时回滚：

```bash
# 回滚 cron 配置到直接调用 Python 脚本
crontab -e
# 改回 docker run ... /music/sync_liked.py

# 或者使用备份
crontab ~/crontab.backup
```

---

## 📝 其他同步任务检查

`sync_playlists.sh` 已经有 flock 保护，但也建议确认 cron 确实在调用它：

```bash
crontab -l | grep sync_playlists
# 应该输出类似：0 */5 * * * /volume1/homes/Mia/Music/sync_playlists.sh
```

---

## 总结

**代码层面：✅ 全部完成**  
**部署层面：⚠️ 需要手动更新 cron 配置**

竞态保护只有在实际调用 `sync_liked.sh`（而非 `sync_liked.py`）时才会生效。
请确认服务器上的 cron 配置已切换完成。
