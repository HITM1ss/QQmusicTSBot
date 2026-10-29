#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "请使用 sudo 运行此脚本。" >&2
  exit 1
fi
project_dir=$(cd "${1:-.}" && pwd -P)
project_name=${2:-$(basename "$project_dir" | tr '[:upper:]' '[:lower:]')}
if [[ ! $project_name =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo "Compose 项目名只能包含小写字母、数字、下划线和连字符。" >&2
  exit 1
fi
for program in docker python3 systemctl; do
  command -v "$program" >/dev/null
done
docker compose version >/dev/null
[[ -f "$project_dir/docker-compose.prebuilt.yml" ]]
[[ -f "$project_dir/scripts/compose-updater.py" ]]

python3 - "$project_dir" "$project_name" <<'PY'
import importlib.util
import json
from pathlib import Path
import shutil
import sys

project = Path(sys.argv[1])
name = sys.argv[2]
if any(character in str(project) for character in ('\n', '\r', '%', '"', '\\')):
    raise SystemExit('项目路径包含不支持的字符')
spec = importlib.util.spec_from_file_location('compose_updater', project / 'scripts/compose-updater.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
config = {'project': str(project), 'name': name}
worker = module.Updater(config)
containers = worker.inspect_services()
backend_env = dict(item.split('=', 1) for item in containers['backend']['Config']['Env'] if '=' in item)
module.version_tuple(backend_env.get('TSBOT_BUILD_VERSION', ''))
if backend_env.get('TSBOT_UPDATER_DIR') != '/app/data/updater':
    raise SystemExit('请先更新预构建 Compose 配置并重建 backend，启用 TSBOT_UPDATER_DIR')
state_dir = project / 'data/updater'
state_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
config_dir = Path('/etc/tsbot-updater')
config_dir.mkdir(mode=0o700, exist_ok=True)
config_path = config_dir / f'{name}.json'
module.atomic_write(config_path, json.dumps(config))
worker_dir = Path('/usr/local/lib/tsbot-updater')
worker_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
worker_path = worker_dir / f'{name}.py'
shutil.copyfile(project / 'scripts/compose-updater.py', worker_path)
worker_path.chmod(0o644)
unit = f'tsbot-updater-{name}'
units = Path('/etc/systemd/system')
service_content, path_content = module.systemd_unit_contents(project, name, worker_path, config_path)
(units / f'{unit}.service').write_text(service_content)
(units / f'{unit}.path').write_text(path_content)
module.activate_systemd_updater(worker, unit, units)
print(f'更新服务已安装。查看日志：journalctl -u {unit}.service')
PY
