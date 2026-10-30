# 版本与一键更新

管理员进入“系统配置 → 版本与更新”，可查看前端和后端各自的构建版本、提交号，并手动检查本仓库的最新稳定 GitHub Release。开发环境显示“开发版”，不参与自动升级。版本号在 CI 构建时写入各自的产物，不依赖当前 Git 标签或浏览器缓存猜测。

## Linux + systemd + Docker Compose

一键更新支持本仓库在 `ghcr.io/hitm1ss/qqmusictsbot-*` 发布的三个预构建镜像，以及 `docker-compose.prebuilt.yml` 的标准目录结构。宿主机需要 Docker Compose 2.36 或更新版本、Python 3 和 systemd，拉取进度使用 Compose 的 `--progress json` 输出。源码构建、历史 `neteasetsbot` 镜像、定制数据目录不自动切换，请先迁移到当前预构建部署并验证数据挂载。

首次启用需要在宿主机完成一次安装：

1. 将包含此功能的 Release 中的 `docker-compose.prebuilt.yml` 和 `scripts/` 放入现有部署目录。保留 `data/`、`logs/`、`tsbot.env` 和原来的 `TSBOT_COOKIE_KEY`。已有数据库应位于部署目录的 `data/tsbot.db`。
2. 检查部署目录 `.env`，将镜像设置为下列地址，并填入实际已发布的版本：

   ```dotenv
   TSBOT_IMAGE_REGISTRY=ghcr.io
   TSBOT_IMAGE_NAMESPACE=hitm1ss
   TSBOT_IMAGE_REPO=qqmusictsbot
   TSBOT_IMAGE_TAG=vX.Y.Z
   ```

3. 在部署目录运行 `docker compose -f docker-compose.prebuilt.yml up -d`，让三个服务都使用带版本信息的新镜像。后端还需要 Compose 中的 `TSBOT_UPDATER_DIR=/app/data/updater`。首次引入更新功能需要这次手动升级。
4. 运行 `sudo bash scripts/install-updater.sh /绝对路径/部署目录`。如果原来使用了 `docker compose -p 自定义项目名`，将相同项目名作为第二个参数传入。

安装脚本会先检查运行中的服务、镜像来源和后端版本，再安装项目专用的 systemd path/service。该服务以 root 调用宿主机 Docker，并只处理指定项目的三个服务；请仅在可信的部署目录安装。后端通过共享数据目录提交版本号和固定的镜像源选项，不需要挂载 Docker socket，也不接收命令、镜像地址或任意更新 URL。

安装期间会记录 `installing`；单元验证、启动或 active 检查失败时记录 `install_failed` 并移除 `installed` 标记，面板不会开放更新按钮。修正宿主机问题后重新运行安装脚本即可。`PathExists` 使用不带引号的绝对路径，兼容 Ubuntu 22.04 的 systemd。

安装成功后，会通过[阿里云公网 NTP 服务](https://help.aliyun.com/en/ecs/user-guide/alibaba-cloud-ntp-server/) `ntp.aliyun.com:123/UDP` 获取一次时间，校准 Linux 宿主机系统时钟，并打印对应的北京时间。校时只在安装流程或手动执行 `sudo python3 scripts/compose-updater.py --sync-time` 时进行；容器共享宿主机时钟，日志格式固定使用 UTC+8。网络校时失败会打印原因，已安装的更新服务继续保留。该操作需宿主机 root 权限，不需要给容器挂载系统时钟权限。

日志页将 TeamSpeak 3 的默认 UTC 服务器日志转换成北京时间，保留原有秒后小数；TeamSpeak 6 如自定义了 `--log-timezone local`，应将服务器日志时区配置回默认 `utc`。旧版机器人文件中的历史时间戳保持原样，新生成的后端、Voice 和辅助脚本日志统一为北京时间。

更新按钮上方的“从何处拉取镜像”提供两行单选项，默认选择“原地址（GHCR）”：

- 原地址（GHCR）：先拉取 GHCR，失败或超过 10 分钟时自动改用南大源。
- 南大源：本次更新直接从 `ghcr.nju.edu.cn` 拉取，最多等待 10 分钟。

选项只作用于本次更新。更新期间不可更改，刷新页面会恢复本次选择；成功后镜像仍使用本仓库的 GHCR 名称，部署目录中的镜像仓库配置保持原样。已有宿主机需复制新脚本并重新运行安装脚本，使源选择生效。

之后在面板点击“检查更新 → 一键更新”，确认所选来源和短暂停机即可。宿主机将：

1. 校验目标版本及三个服务的镜像来源，拒绝降级。
2. 从所选来源拉取三个目标版本镜像；选择原地址时保留上述南大源回退。南大源的三个镜像全部成功后，才在本地恢复为原 GHCR 镜像名。如果拉取失败，更新结束，当前服务继续运行。
3. 停止后端写入，使用 SQLite backup 将数据库备份到 `data/backups/`。
4. 将目标版本写入部署目录 `.env` 的 `TSBOT_IMAGE_TAG`，保留其他变量，重建三个容器。
5. 检查容器运行状态、后端版本及 HTTP 接口、前端提供的 `version.json`。通过后面板显示完成，刷新页面即可加载新前端。

更新阶段可能暂时无法访问网页。面板会显示 GHCR 超时、切换南大源及最终使用的来源。面板仅在更新进行中每 3 秒刷新一次进度，最多等待 35 分钟；systemd 的总任务期限也是 35 分钟，为两轮拉取和后续重建检查留出时间。重新打开页面也能读取保存在磁盘上的结果。

拉取期间显示“正在拉取更新镜像（vX.Y.Z）...23%”。百分比根据 [Docker Compose 的 JSON 进度输出](https://docs.docker.com/reference/cli/docker/compose/)估算：三个镜像等权汇总，每个镜像内按已知镜像层的下载字节计算；已缓存镜像直接计为完成，解压字节不混入下载量。100% 表示拉取命令成功结束，随后继续显示备份、重建和检查阶段。切换南大源时重新统计这一轮拉取进度，文案同时显示切换原因与来源。更新宿主机脚本并重新运行安装脚本后，这项进度采集才会生效。

南大源地址使用其[GHCR 镜像说明](https://doc.nju.edu.cn/books/e1654/page/ghcr)中的域名替换规则。重试只针对镜像拉取阶段；服务已重建后的就绪检查失败，仍按下述失败与恢复流程处理。

## 失败与恢复

以部署目录名 `tsbot` 为例，宿主机日志为：

```bash
sudo journalctl -u tsbot-updater-tsbot.service -n 100 --no-pager
```

如果备份前失败，旧后端会重新启动，镜像版本不切换。如果已重建容器但检查失败，面板会明确显示失败，并保留选定版本供诊断；不会擅自用旧数据库覆盖新版本可能写入的数据。需要回退时先停止后端，保留现有数据库副本，再恢复对应的 `data/backups/tsbot-before-*.db`，将 `.env` 的版本改回先前版本后重新运行 Compose。运行中的数据库及其 WAL 文件不要直接覆盖。

停用更新入口：

```bash
sudo systemctl disable --now tsbot-updater-tsbot.path
sudo rm data/updater/installed
```

如果更新服务代码本身有变化，将新脚本复制到部署目录后重新运行安装脚本。脚本副本与 systemd 单元由宿主机维护，不随容器重建变化。
