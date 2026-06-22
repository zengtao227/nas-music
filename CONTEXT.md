---
project: nas-music
type: infrastructure
status: active
nas_path: /volume1/docker/
nas_host: nas (192.168.68.68)
public_url: https://music.zengsg.dpdns.org
---

# NAS Music Server

Mia 的自托管音乐流媒体系统。替代 Spotify Premium，音乐文件存在 NAS，支持局域网和外网播放。

## 架构

```
Spotify（Mia 的免费账号）
    ↓ Liked Songs 通过 sp_dc cookie 授权
sync_liked.py（spotapi + spotDL）每 5 分钟自动同步
    ↓
NAS /volume1/homes/Mia/Music/
    ↓ Jellyfin 媒体服务器（端口 8096）
Finamp（Mia 的 Android 手机 + Samsung 平板）
    ↓ 外网通过
Cloudflare Tunnel → https://music.zengsg.dpdns.org
```

## 同步机制

同步源直接是 Mia 的 **Liked Songs**（不经过任何中间歌单）：
- Mia 在 Spotify 点 Like → 最多 5 分钟后歌曲自动下载到 NAS → 出现在 Finamp
- Mia 取消 Like → 最多 5 分钟后从 NAS 删除
- 同一首歌的不同版本（Slowed / Sped Up / Holiday Version）视为独立歌曲，都会被下载

同步脚本：`/volume1/homes/Mia/Music/sync_liked.py`（在 Docker 容器内运行）
Shell 入口：`/volume1/homes/Mia/Music/sync_liked.sh`
存档文件：`/volume1/homes/Mia/Music/liked.spotdl`（spotDL 用于追踪哪些歌已下载）
同步日志：`/volume1/homes/Mia/Music/.spotdl_liked_sync.log`

`/etc/crontab` 条目：
```
*/5  *  *  *  *  root  bash /volume1/homes/Mia/Music/sync_liked.sh
```

## 组件

| 组件 | 位置 | 说明 |
|------|------|------|
| Jellyfin | `/volume1/docker/jellyfin/` | 媒体服务器，端口 8096 |
| spotDL 镜像 | `spotdl-local:latest` | 自建镜像，修复了 SpotipyFree ownerV2/name/uri bug |
| cloudflared | `/volume1/docker/cloudflared/` | Cloudflare Tunnel，外网访问 |
| 音乐文件 | `/volume1/homes/Mia/Music/` | 按 `{artists}/{album}/{title}` 组织 |
| Cookie 检测 | `/volume1/homes/Mia/Music/check_cookie.sh` | 每日 08:00 检测，失效时发 Telegram 通知 |

## spotDL Docker 镜像（spotdl-local:latest）

**构建文件**：`/volume1/docker/spotdl/`
- `Dockerfile`：基于 python:3.11-slim，安装 ffmpeg + spotDL
- `patch_spotipyfree.py`：修复 SpotipyFree 三个 bug（ownerV2 / name / uri 键缺失）

重新构建命令：
```bash
sudo /usr/local/bin/docker build --no-cache -t spotdl-local:latest /volume1/docker/spotdl/
```

## Cloudflare Tunnel

- **Tunnel 名称**：home-nas
- **Tunnel ID**：dd639dbe-4be1-4969-b9fd-e46b03265bf2
- **配置文件**：`/volume1/docker/cloudflared/config.yml`
- **DNS**：`music.zengsg.dpdns.org` CNAME → `dd639dbe-4be1-4969-b9fd-e46b03265bf2.cfargotunnel.com`

重启：`cd /volume1/docker/cloudflared && sudo /usr/local/bin/docker compose restart`

## Jellyfin 设置

- **管理员**：zengtao227（NAS 管理员密码）
- **Mia 账号**：只有 Music 媒体库访问权，无删除权限
- **音乐库路径**：`/media/music`（容器内），对应 `/volume1/homes/Mia/Music/`
- **实时监控**：开启（新文件自动入库，无需手动扫描）

## Finamp 配置（Mia 手机）

- **Server URL（在家）**：`http://192.168.68.68:8096`
- **Server URL（外网）**：`https://music.zengsg.dpdns.org`
- **账号**：Mia（Jellyfin 账号，非 Spotify）

---

## Spotify sp_dc Cookie 管理

### 什么是 sp_dc

Spotify 的浏览器会话 cookie。NAS 脚本用它以 Mia 的身份访问 Liked Songs，无需 Spotify Premium，也无需任何 Spotify 开发者 API 密钥。

### 存储位置

`/volume1/homes/Mia/Music/.spotify_sp_dc`（NAS，root 所有，权限 600）

### 有效期与失效条件

- 正常有效期：1-2 年
- 提前失效：Mia 在浏览器中退出 Spotify 登录、修改密码

### 如何获取 sp_dc（从浏览器复制）

1. 在电脑浏览器打开 [open.spotify.com](https://open.spotify.com)，确认已登录 **Mia 的账号**
2. 打开开发者工具：
   - **Mac**：`Command + Option + I`
   - **Windows / Linux**：`F12`
3. 点击顶部 **Application** 标签（Chrome / Edge）或 **Storage** 标签（Firefox）
4. 左侧展开 **Cookies** → 点击 `https://open.spotify.com`
5. 在右侧列表找到名称为 **`sp_dc`** 的行，复制 **Value** 列的完整字符串（约 270 个字符）

### 更新 Cookie 到 NAS

SSH 连接 NAS 后执行（把 `新的sp_dc值` 替换为刚才复制的字符串）：

```bash
sudo /usr/local/bin/docker run --rm \
  -v /volume1/homes/Mia/Music:/music \
  --entrypoint sh \
  spotdl-local:latest \
  -c 'echo "新的sp_dc值" > /music/.spotify_sp_dc && chmod 600 /music/.spotify_sp_dc && echo OK'
```

更新后等待下次 5 分钟 cron 自动验证。

### 每日有效性检测与通知

每日 08:00 cron 自动运行 `check_cookie.sh`：
- 读取 `.spotify_sp_dc`，向 Spotify 发一次测试请求
- Cookie 有效：静默通过
- Cookie 失效：通过 **@VPN_frank_bot**（Telegram）发送告警消息

Bot 的 Token 和 Chat ID 存储在 `check_cookie.sh` 中，不在此处记录。
需要修改通知目标时，直接编辑 `/volume1/homes/Mia/Music/check_cookie.sh`。

`/etc/crontab` 条目：
```
0  8  *  *  *  root  bash /volume1/homes/Mia/Music/check_cookie.sh
```
