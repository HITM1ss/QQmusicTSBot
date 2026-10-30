# Repository Guidelines（仓库指南）

## 项目结构与模块组织

- `backend/`：Python/FastAPI API、SQLite 数据模型、QQ 音乐适配器（`qqmusic.py`）和 Voice gRPC 客户端。
- `web/`：Vue 3 + TypeScript + Vite 控制台；页面在 `web/src/views/`，可复用组件在 `web/src/components/`。
- `voice-service/`：Rust TeamSpeak 音频客户端和 gRPC 服务；协议定义在 `proto/`。
- `tests/`：后端回归、鉴权和 QQ 音乐行为测试；`vendor/` 为固定的本地 Rust crate。

当前音乐源仅为 QQ 音乐。新增点歌、搜索或歌单逻辑应复用 `backend/qqmusic.py`，并使用 `qqmusic:<song_mid>` 作为曲目 ID；不要重新引入已退役音乐源的依赖、路由或界面。

网页区分管理员与访客会话；新增路由必须同步检查 `backend/auth.py` 的访客白名单与前端权限。访客仅可浏览和加入 QQ 音乐队列，管理设置、授权、更新与全局播放控制必须验证管理员身份；网页写操作验证 CSRF。`/external/*` 始终要求已配置的 Token，兼容 Bearer 与 `x-api-token`，不要把 Token 打包到共享前端。

新日志显式按北京时间（UTC+8）格式化，不依赖容器或浏览器本地时区。宿主机网络校时仅由 Linux 安装流程或 `--sync-time` 执行；测试必须 mock 系统时钟写入，禁止在开发机测试时修改真实系统时间。

## 构建、测试与开发命令

- `make backend-setup`：创建 `backend/.venv` 并安装 Python 依赖。
- `npm --prefix web ci && npm --prefix web run build`：安装并构建前端生产包。
- `cargo build --manifest-path voice-service/Cargo.toml`：构建语音服务；`make all` 执行完整构建。
- `backend/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`：运行后端测试。
- 开发时按需运行 `backend/.venv/bin/uvicorn backend.main:app --reload`、`npm --prefix web run dev` 和 `./run-voicemake.sh`。使用 `docker compose up -d --build` 验证容器栈。

## 编码与测试约定

Python 使用 4 空格、类型标注、`snake_case` 函数和 `PascalCase` 类。Vue/TypeScript 使用现有 2 空格风格、`camelCase` 值和 `PascalCase.vue` 组件。Rust 提交前执行 `cargo fmt`；不要手动编辑 `backend/_generated/` 等生成文件。

在 `tests/` 添加 `test_*.py` 回归测试；测试类以 `Tests` 结尾，测试方法以 `test_` 开头。使用 mock、临时 SQLite 数据库和临时文件，避免依赖真实 QQ 音乐 API 或 TeamSpeak。修改点歌、队列或历史重播时，应覆盖 Cookie 缺失、歌曲不可播放和自动跳过等回归场景。前端或语音服务变更至少分别通过前端构建和 Rust 构建。

## 运行时配置

从 `tsbot.env.example` 复制出 `tsbot.env`，并在部署前设置高强度 `TSBOT_COOKIE_KEY`。TeamSpeak 连接、QQ 音乐登录态、外部 API Token 和界面资源由管理员设置页管理。升级代码时保留旧数据库的兼容路径；若改动队列序列化、播放地址或配置键，必须说明迁移行为，并确保旧队列不会阻塞后续 QQ 音乐播放。

## 变更检查

提交前确认 API 路由、前端调用和文档中的来源名称保持一致。修改 `/external/*` 接口时，兼顾 Bearer Token 与 `x-api-token` 两种认证方式。涉及 Dockerfile 或依赖清单时，避免添加未使用的大型运行时依赖；镜像变更应至少通过一次本地构建或 CI 构建验证。合并前运行 `git diff --check`，并在 PR 中记录实际执行过的构建与测试命令；同步检查 `README.md`、`README.en.md`、`web/README.md` 和 `AGENTS.md`。

## 提交、安全与 PR

使用 Conventional Commit，例如 `feat(player): ...`、`fix(qqmusic): ...`。PR 应说明影响、测试结果、配置迁移和界面截图。不要提交 `tsbot.env`、Cookie、Token、数据库、日志或任何脱敏前的诊断输出；Issue 和 PR 中的播放链接、Cookie 片段、用户信息和日志也必须先脱敏。
