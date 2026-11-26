#!/bin/bash
# 在目标机 14.103.80.52 上以 root 执行。
# 用法：bash deploy.sh
#
# 做的事：
#   1. 建独立系统用户 campus-proto（无登录 shell），原型以它运行，和中转站的进程、文件权限隔离。
#   2. 把仓库里的后端与前端文件同步到 /opt/campus-prototype，已有代码和数据库先备份。
#   3. 准备 /etc/campus-prototype/env（首次运行从 env.example 生成，不覆盖已有配置）。
#   4. 装 systemd unit，campus-prototype.service 跑在 127.0.0.1:8080，带资源上限和沙箱限制。
#   5. 提示下一步：把 nginx 片段接入 campusgo.aihuanxi.ai 的 server 块。
#
# 不做：不改 nginx 主配置（避免误改中转站），不重启 nginx，不碰 New-API。
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 执行" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/campus-prototype
CONF_DIR=/etc/campus-prototype
DATA_DIR=/var/lib/campus-prototype
SERVICE_USER=campus-proto

echo "==> 1/5 建系统用户 ${SERVICE_USER}"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "==> 2/5 同步后端代码到 ${APP_DIR}"
PY_FILES="server.py app_server.py config.py authz.py identity.py migrations.py migrate.py importer.py analytics.py onboarding.py works_service.py org_service.py relay.py roster.py pat_service.py rewards_service.py"
WEB_FILES="index.html overview.html student-portal.html college-dashboard.html school-ops.html platform-admin.html tailwindcss.min.js admin.html teacher.html import.html gallery.html app.js admin.js teacher.js import.js gallery.js components.js agent-tools.js styles.css"
mkdir -p "${APP_DIR}"
if [ -n "$(ls -A "${APP_DIR}" 2>/dev/null)" ]; then
  BACKUP_DIR="${APP_DIR}.bak-$(date +%Y%m%d-%H%M%S)"
  cp -a "${APP_DIR}" "${BACKUP_DIR}"
  echo "    已备份旧代码到 ${BACKUP_DIR}"
fi
if [ -d "${DATA_DIR}" ] && [ -n "$(ls -A "${DATA_DIR}" 2>/dev/null)" ]; then
  DATA_BACKUP="${DATA_DIR}.bak-$(date +%Y%m%d-%H%M%S)"
  cp -a "${DATA_DIR}" "${DATA_BACKUP}"
  echo "    已备份数据库到 ${DATA_BACKUP}，迁移会在已有库上运行。"
fi
for f in ${PY_FILES} ${WEB_FILES}; do
  install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0640 "${SRC_DIR}/${f}" "${APP_DIR}/${f}"
done
if [ -d "${SRC_DIR}/demo-media" ]; then
  mkdir -p "${APP_DIR}/demo-media"
  chown "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}/demo-media"
  chmod 0750 "${APP_DIR}/demo-media"
  for img in "${SRC_DIR}"/demo-media/*; do
    [ -f "${img}" ] && install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0640 "${img}" "${APP_DIR}/demo-media/$(basename "${img}")"
  done
fi

echo "==> 3/5 准备配置目录 ${CONF_DIR} 和数据目录 ${DATA_DIR}"
mkdir -p "${CONF_DIR}" "${DATA_DIR}"
chown "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}"
chmod 0750 "${DATA_DIR}"
if [ ! -f "${CONF_DIR}/env" ]; then
  install -m 0640 -o root -g "${SERVICE_USER}" "${SRC_DIR}/deploy/env.example" "${CONF_DIR}/env"
  echo "    已生成 ${CONF_DIR}/env，里面的 ADMIN_USERNAME/ADMIN_PASSWORD 是占位值，上线前必须手动改成真实值。"
else
  echo "    ${CONF_DIR}/env 已存在，不覆盖。"
fi
chmod 0750 "${CONF_DIR}"

echo "==> 4/5 安装 systemd unit"
install -m 0644 "${SRC_DIR}/deploy/campus-prototype.service" /etc/systemd/system/campus-prototype.service
systemctl daemon-reload
systemctl enable --now campus-prototype.service
sleep 1
systemctl --no-pager --full status campus-prototype.service || true

echo "==> 5/5 后端自检"
curl -fsS http://127.0.0.1:8080/api/health && echo " <- 后端健康检查通过" || echo "后端健康检查失败，请看 journalctl -u campus-prototype"

cat <<'NEXT'

部署脚本到此结束，接下来手动做，不自动改 nginx：

  1. 编辑 /etc/campus-prototype/env，把 ADMIN_USERNAME/ADMIN_PASSWORD 改成真实值，然后
     systemctl restart campus-prototype
  2. 备份现有 nginx 配置：
     cp /etc/nginx/sites-enabled/<campusgo对应的文件> /etc/nginx/sites-enabled/<同名>.bak-$(date +%Y%m%d)
  3. 复制 deploy/nginx-campus-http.conf 到 /etc/nginx/conf.d/campus-http.conf（http 级：限流区和 upstream）
     复制 deploy/campus-proxy-common.conf 到 /etc/nginx/campus-proxy-common.conf
     复制 deploy/nginx-campus-locations.conf 到 /etc/nginx/campus-locations.conf
  4. 在 campusgo.aihuanxi.ai 的 server 块里加一行 include /etc/nginx/campus-locations.conf;
     （写在 location / 之前，否则会被根路径的中转站规则先匹配掉）
  5. nginx -t 通过后 systemctl reload nginx
  6. curl -I https://campusgo.aihuanxi.ai/app/ 确认 200，并抽查 /app/api/health
NEXT
