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
    ↓ Liked Songs + 私有歌单 通过 sp_dc cookie 授权
sync_liked.py / sync_playlists.py（spotapi + spotDL）每 5 分钟自动同步
    ↓
NAS /volume1/homes/Mia/Music/
    ↓ Jellyfin 媒体服务器（端口 8096）
Finamp（Mia 的 Android 手机 + Samsung 平板）
    ↓ 外网通过
Cloudflare Tunnel → https://music.zengsg.dpdns.org
```

## 文件结构

```
/volume1/homes/Mia/Music/
├── {artists}/{album}/{title}.mp3     ← Liked Songs（直接在根目录展开）
├── liked.spotdl                      ← Liked Songs 追踪文件
├── sync_liked.py / sync_liked.sh     ← Liked Songs 同步脚本
├── sync_playlists.py / sync_playlists.sh  ← 私有歌单同步脚本
├── check_cookie.sh                   ← cookie 有效性检测脚本
└── Playlists/
    ├── summer26/
    │   ├── summer26.spotdl           ← Summer 26 追踪文件
    │   └── {artists}/{album}/{title}.mp3
    └── can_dances/
        ├── can_dances.spotdl         ← Can Dances 追踪文件
        └── {artists}/{album}/{title}.mp3
```

**注意**：三个来源（Liked Songs / Summer 26 / Can Dances）的文件存在不同目录，磁盘上不重复。若同一首歌既被 Like 又在歌单里，则会存两份文件。

## 同步机制

### Liked Songs（每 5 分钟）

同步源直接是 Mia 的 **Liked Songs**（不经过任何中间歌单）：
- Mia 在 Spotify 点 Like → 最多 5 分钟后歌曲自动下载到 NAS → 出现在 Finamp
- Mia 取消 Like → 最多 5 分钟后从 NAS 删除
- 同一首歌的不同版本（Slowed / Sped Up / Holiday Version）视为独立歌曲，都会被下载

| 文件 | 路径 |
|------|------|
| 同步脚本（Docker 内运行） | `/volume1/homes/Mia/Music/sync_liked.py` |
| Shell 入口（带 lockfile） | `/volume1/homes/Mia/Music/sync_liked.sh` |
| 追踪文件 | `/volume1/homes/Mia/Music/liked.spotdl` |
| 日志 | `/volume1/homes/Mia/Music/.spotdl_liked_sync.log` |

### 私有歌单（每 5 分钟）

同步 Mia 的两个私有 Spotify 歌单，支持歌单设为 private 状态下正常运作：
- 新增歌曲 → 最多 5 分钟后下载到对应子目录
- 删除歌曲 → 从歌单追踪文件中移除，本地文件同步删除

| 歌单 | Spotify ID | 本地目录 |
|------|-----------|---------|
| Summer 26 | `3ebskb0Uy9zbm87SyemHjG` | `Playlists/summer26/` |
| Can Dances | `2Rx94JQDRIft0V4Fd9rMq5` | `Playlists/can_dances/` |

| 文件 | 路径 |
|------|------|
| 同步脚本（Docker 内运行） | `/volume1/homes/Mia/Music/sync_playlists.py` |
| Shell 入口（带 lockfile） | `/volume1/homes/Mia/Music/sync_playlists.sh` |
| 日志 | `/volume1/homes/Mia/Music/.spotdl_playlists_sync.log` |

**私有歌单认证方式**：`spotapi.PublicPlaylist(playlist_id, client=login.client)` — 将 sp_dc 认证后的 TLS client 注入 PublicPlaylist，使其可访问私有歌单。

### Crontab（`/etc/crontab`）

```
*/5  *  *  *  *  root  bash /volume1/homes/Mia/Music/sync_liked.sh
*/5  *  *  *  *  root  bash /volume1/homes/Mia/Music/sync_playlists.sh
0    8  *  *  *  root  bash /volume1/homes/Mia/Music/check_cookie.sh
```

## Jellyfin

- **管理员**：zengtao227（NAS 管理员密码）
- **Mia 账号**：只有 Music 媒体库访问权，无删除权限
- **音乐库名称**：Music（曾是"音乐"，已通过 API 改名）
- **音乐库路径**：`/media/music`（容器内），对应 `/volume1/homes/Mia/Music/`
- **实时监控**：开启（新文件自动入库，无需手动扫描）

### Jellyfin 播放列表

已在 Jellyfin 中创建两个播放列表，Finamp 的 Playlists 标签可直接看到：

| 播放列表 | Jellyfin ID |
|---------|------------|
| Summer 26 | `ed82387a29c7bf3d4703b7d964d94c54` |
| Can Dances | `313dc8185ed60db38a6a6b42e2321835` |

**注意**：新歌加入歌单后，sync_playlists.py 会下载文件、Jellyfin 实时扫描会将其入库，但 Jellyfin 播放列表不会自动更新。如需更新播放列表，需重新调用 Jellyfin API（见下方 API 操作记录）。

### Jellyfin API 操作

管理员 API Token（存储在 Jellyfin DB，不在此处记录 token 值）：
- 创建方式：`sqlite3 /volume1/docker/jellyfin/config/data/jellyfin.db "INSERT INTO ApiKeys ..."`
- 管理员 UserId：`421797B3-C2CC-4133-9233-9334AD62ABD2`
- Mia UserId：`9DBDBD21-920F-49E0-86B0-AC5D26D2C63B`

重命名媒体库：
```bash
curl -X POST 'http://localhost:8096/Library/VirtualFolders/Name?name=旧名&newName=新名&refreshLibrary=false' \
  -H 'X-MediaBrowser-Token: <token>' -H 'Content-Length: 0'
```

## 组件

| 组件 | 位置 | 说明 |
|------|------|------|
| Jellyfin | `/volume1/docker/jellyfin/` | 媒体服务器，端口 8096 |
| spotDL 镜像 | `spotdl-local:latest` | 自建镜像，修复了 SpotipyFree ownerV2/name/uri bug |
| cloudflared | `/volume1/docker/cloudflared/` | Cloudflare Tunnel，外网访问 |
| 音乐文件 | `/volume1/homes/Mia/Music/` | 见「文件结构」 |
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

## Finamp 配置（Mia 手机 + 平板）

- **Server URL（在家）**：`http://192.168.68.68:8096`
- **Server URL（外网）**：`https://music.zengsg.dpdns.org`
- **账号**：Mia（Jellyfin 账号，非 Spotify）
- **重要**：正常使用不需要退出登录；退出后 Finamp 不记住凭据，需重新手动输入。
  如果只是需要刷新数据，用 Finamp 设置里的「重新连接」而不是退出登录。

---

## Spotify sp_dc Cookie 管理

### 什么是 sp_dc

Spotify 的浏览器会话 cookie。NAS 脚本用它以 Mia 的身份访问 Liked Songs 和私有歌单，无需 Spotify Premium，也无需任何 Spotify 开发者 API 密钥。

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
