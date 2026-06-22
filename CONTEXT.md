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
    ↓ spotDL 每 5 分钟自动同步
NAS /volume1/homes/Mia/Music/
    ↓ Jellyfin 媒体服务器
Finamp（Mia 的 Android 手机 + Samsung 平板）
    ↓ 外网通过
Cloudflare Tunnel → https://music.zengsg.dpdns.org
```

## 组件

| 组件 | 位置 | 说明 |
|------|------|------|
| Jellyfin | `/volume1/docker/jellyfin/` | 媒体服务器，端口 8096 |
| spotDL 镜像 | `spotdl-local:latest` | 自建镜像，修复了 SpotipyFree ownerV2/name/uri bug |
| cloudflared | `/volume1/docker/cloudflared/` | Cloudflare Tunnel，外网访问 |
| 音乐文件 | `/volume1/homes/Mia/Music/` | 按 `{artists}/{album}/{title}` 组织 |
| 同步脚本 | `/volume1/homes/Mia/Music/sync_music.sh` | 每 5 分钟自动运行 |

## Spotify 歌单

- **同步目标**：Downloads 歌单（公开）
- **URL**：`https://open.spotify.com/playlist/7w0NXz1K8ymcgRojHJ24jo`
- **说明**：Liked Songs 因 Spotify Free 限制无法直接同步，Mia 需手动加歌到 Downloads 歌单

## 自动同步

`/etc/crontab` 条目（通过 Docker 写入，`/usr/sbin/crond` 执行）：
```
*/5  *  *  *  *  root  bash /volume1/homes/Mia/Music/sync_music.sh
```

同步日志：`/volume1/homes/Mia/Music/.spotdl_sync.log`

sync 命令是智能的：只下载新歌，自动删除从歌单移除的歌，不重复下载。

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
- **docker-compose**：`/volume1/docker/cloudflared/docker-compose.yml`
- **DNS**：`music.zengsg.dpdns.org` CNAME → `dd639dbe-4be1-4969-b9fd-e46b03265bf2.cfargotunnel.com`

重启 Tunnel：
```bash
cd /volume1/docker/cloudflared && sudo /usr/local/bin/docker compose restart
```

## Jellyfin 设置

- **管理员**：zengtao227（NAS 管理员密码）
- **Mia 账号**：只有 Music 媒体库访问权，无删除权限
- **音乐库路径**：`/media/music`（容器内），对应 `/volume1/homes/Mia/Music/`
- **元数据**：MusicBrainz + TheAudioDB，地区设置美国
- **实时监控**：开启（新文件自动入库）

## Finamp 配置（Mia 手机）

- **Server URL（在家）**：`http://192.168.68.68:8096`
- **Server URL（外网）**：`https://music.zengsg.dpdns.org`
- **账号**：Mia（Jellyfin 账号，非 Spotify）

## Mia 的使用方式

| 动作 | 操作 |
|------|------|
| 加新歌 | Spotify → Downloads 歌单 → Add（最多 5 分钟后出现在 Finamp） |
| 删歌 | Spotify → Downloads 歌单 → Remove（最多 5 分钟后从 NAS 删除） |
| 听歌 | 打开 Finamp |

## 初始导入（待完成）

Mia 的 Liked Songs（779 首）还未加入 Downloads 歌单。
需在 Spotify 桌面版：Liked Songs → Ctrl+A 全选 → Add to playlist → Downloads。
