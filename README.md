# TSBot（QQ 音乐版）

TSBot 是一个面向 TeamSpeak 的音乐机器人。后端通过 QQ 音乐获取歌曲、歌单、歌词和播放地址，voice-service 负责连接 TeamSpeak 并播放音频。

> 仓库目录、历史镜像名中可能保留 NeteaseTSBot 字样，仅为兼容既有部署；当前版本的音乐源只有 QQ 音乐。

## 功能

- TeamSpeak TS3/TS6 音频播放、音量、随机、循环、切歌与进度控制
- QQ 音乐搜索、歌单、歌词、专辑、歌手和 MV 查询
- QQ 音乐扫码或 Cookie 授权；凭据加密保存在服务端
- 播放队列、最近播放、本地收藏和 Web 控制台
- TeamSpeak 聊天点歌、歌单搜索和外部 API 集成

## 架构

~~~text
[web: Vue 3] --HTTP--> [backend: FastAPI + QQ Music] --gRPC--> [voice-service: Rust] --> TeamSpeak
~~~

项目主要目录：

- backend/：FastAPI、QQ 音乐适配器、数据库和语音客户端
- web/：Vue 3 + TypeScript 管理控制台
- voice-service/：Rust TeamSpeak 音频服务
- tests/：后端回归测试
- proto/：gRPC 协议定义

## 快速开始

系统需要 Linux、Python 3.10+、Node.js 16+、Rust 1.70+、构建工具和 ffmpeg。

~~~bash
cp tsbot.env.example tsbot.env
# 在 tsbot.env 中设置高强度 TSBOT_COOKIE_KEY
make all
~~~

首次启动后，后端会生成一次性管理员密码并写入 logs/initial-admin-password.txt。登录 Web 后请立即修改密码，并在“系统配置 → 音乐会员登录”完成 QQ 音乐扫码或填写 Cookie。

本地开发可分别启动三个服务：

~~~bash
./run-voicemake.sh
backend/.venv/bin/uvicorn backend.main:app --reload --host 127.0.0.1 --port 8009
npm --prefix web run dev
~~~

开发前端默认访问 http://127.0.0.1:5173，后端 OpenAPI 为 http://127.0.0.1:8009/docs。

## Docker 部署

~~~bash
cp tsbot.env.example tsbot.env
# 设置 TSBOT_COOKIE_KEY
docker compose up -d --build
docker compose ps
~~~

服务默认暴露 web 8080、backend 8009 和 voice-service gRPC 50051。查看日志：

~~~bash
docker compose logs -f backend
docker compose logs -f web
docker compose logs -f voice-service
~~~

如需使用 GitHub Actions 发布的预构建镜像：

~~~bash
docker compose -f docker-compose.prebuilt.yml up -d
# 固定到某个 GitHub Release 对应的镜像版本：
TSBOT_IMAGE_TAG=vX.Y.Z docker compose -f docker-compose.prebuilt.yml up -d
~~~

推送到 `main` 或 `master` 且命中发布路径时，Docker Publish 会构建三个服务镜像；全部成功后会自动递增最新稳定 `vX.Y.Z` 标签的补丁版本、创建 GitHub Release，并推送同名镜像标签。镜像仓库名可能沿用历史命名；切换镜像前请确认 docker-compose.prebuilt.yml 中的 namespace、registry 和 tag。

“系统配置 → 版本与更新”显示前后端版本并支持检查更新。Linux + systemd 的预构建 Compose 部署可安装宿主机更新服务后使用一键更新，详见 [更新说明](docs/UPDATING.md)。预构建 Compose 默认使用本仓库的 GHCR 镜像。

## QQ 音乐授权与点歌

QQ 音乐播放地址和用户歌单通常需要有效登录态。管理员可在 Web 控制台完成扫码，或通过管理员接口保存 Cookie：

- GET /admin/qqmusic/status：检查 Cookie 是否已配置
- POST /admin/qqmusic/cookie：保存 Cookie
- DELETE /admin/qqmusic/cookie：删除保存的登录信息
- POST /admin/qqmusic/qr/confirm：确认扫码并保存登录态

TeamSpeak 聊天支持以下常用命令：

| 指令 | 示例 | 行为 |
| --- | --- | --- |
| 搜索 / search | 搜索 稻香 | 搜索 QQ 音乐歌曲 |
| 增加 / add | 增加 稻香 | 加入队列 |
| 点歌 | 点歌 稻香 | 置顶为下一首，不打断当前歌曲 |
| 播放 / play | 播放 稻香 | 立即播放 |
| 歌单 / playlist | 歌单 周杰伦 | 搜索 QQ 音乐歌单 |
| 选择 / select | 选择 1 | 将歌单曲目加入队列 |
| 清空 / clear | 清空 | 清空队列并停止播放 |

歌曲受版权、VIP 或 Cookie 失效影响时可能无法播放；机器人会跳过不可播放项并继续后续队列。

## 外部 API

配置 TSBOT_API_TOKEN 或 TSBOT_API_TOKENS 后，/external/* 接口需要 Authorization: Bearer token 或 x-api-token 请求头。

- GET /external/status：读取状态与队列预览
- GET /external/search?source=qqmusic&keywords=稻香：搜索 QQ 音乐
- POST /external/queue：按 song_mid 或关键词点歌
- GET /external/history：读取历史
- POST /external/history/{history_id}/replay：重播 QQ 音乐历史项

接口的实时定义以后端 OpenAPI 文档为准。

## 构建与测试

~~~bash
make backend-setup
npm --prefix web ci && npm --prefix web run build
cargo build --manifest-path voice-service/Cargo.toml
backend/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
~~~

## 升级说明

升级到当前版本时，已退役来源遗留在播放队列中的项目会在后端启动时自动移除，避免阻塞后续播放。旧历史记录不会再被重新点播；建议在确认升级成功后按需清理。

## 安全

- 不要提交 tsbot.env、Cookie、Token、数据库或日志。
- 管理员 Cookie 与初始密码文件应视为敏感信息。
- 对外开放 backend 时请配置 API Token，并通过反向代理或防火墙限制管理入口。
