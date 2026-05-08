#!/usr/bin/env bash
# 一键引导脚本：在干净的 Debian/Ubuntu VPS 上准备 okx-trade paper trading 环境。
#
# 用法（root 或 sudo 用户）::
#
#     curl -fsSL https://raw.githubusercontent.com/<you>/okx-trade/main/deploy/bootstrap.sh | sudo bash
#     # 或本地：sudo bash deploy/bootstrap.sh
#
# 假设：Debian 12 / Ubuntu 22.04+。其他发行版请手动改 apt → dnf/zypper/...。
#
# 步骤：
#   1. 装 Python 3.12+ + git + 构建工具
#   2. 创建 okxtrade 用户 + clone 仓库到 /home/okxtrade/okx-trade
#   3. 装 venv + 安装 .[strategy]
#   4. 提示填 .env
#   5. 装 systemd unit + timer
#   6. 等用户 systemctl start okx-trade
set -euo pipefail

REPO_URL="${REPO_URL:-}"   # 部署时设置；或部署时手动 git clone
APP_USER="${APP_USER:-okxtrade}"
APP_HOME="/home/${APP_USER}"
APP_DIR="${APP_HOME}/okx-trade"
VENV="${APP_DIR}/.venv"

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "ERROR: this script must run as root (or via sudo)" >&2
    exit 1
  fi
}

step() { echo -e "\n\033[1;36m==> $*\033[0m"; }

require_root

step "[1/6] Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# Ubuntu 24.04 默认 Python 3.12；22.04 需要 deadsnakes PPA 或 conda 装新版
apt-get install -y \
  python3 python3-venv python3-pip \
  git curl ca-certificates build-essential \
  pkg-config libssl-dev

# 检查 Python 版本
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [[ $PY_MAJOR -lt 3 || ( $PY_MAJOR -eq 3 && $PY_MINOR -lt 12 ) ]]; then
  echo "ERROR: detected Python ${PY_VER}; need >=3.12 (NT 1.226 stubs use PEP 695 type statement)." >&2
  echo "       Install python3.12 manually (deadsnakes PPA or pyenv) and re-run." >&2
  exit 2
fi
echo "Python ${PY_VER} OK"

step "[2/6] Creating ${APP_USER} user + checking out repo"
if ! id -u "${APP_USER}" &>/dev/null; then
  adduser --system --group --home "${APP_HOME}" --shell /bin/bash "${APP_USER}"
fi

if [[ ! -d "${APP_DIR}/.git" ]]; then
  if [[ -z "${REPO_URL}" ]]; then
    echo "WARN: REPO_URL not set; skipping clone." >&2
    echo "      Manually copy/clone the repo to ${APP_DIR} (chown ${APP_USER}:${APP_USER}) and re-run." >&2
  else
    sudo -u "${APP_USER}" git clone "${REPO_URL}" "${APP_DIR}"
  fi
fi

step "[3/6] Setting up Python venv + installing okx-trade"
if [[ -d "${APP_DIR}" ]]; then
  if [[ ! -d "${VENV}" ]]; then
    sudo -u "${APP_USER}" python3 -m venv "${VENV}"
  fi
  sudo -u "${APP_USER}" "${VENV}/bin/pip" install --upgrade pip wheel
  sudo -u "${APP_USER}" "${VENV}/bin/pip" install -e "${APP_DIR}[strategy]"
  echo "venv ready at ${VENV}"
else
  echo "WARN: ${APP_DIR} not present; skipping venv install (run again after manual clone)" >&2
fi

step "[4/6] Checking .env"
if [[ ! -f "${APP_DIR}/.env" ]]; then
  if [[ -f "${APP_DIR}/.env.example" ]]; then
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
    chmod 600 "${APP_DIR}/.env"
  fi
  echo "ACTION REQUIRED: edit ${APP_DIR}/.env with your OKX demo creds before starting"
fi

step "[5/6] Installing systemd units"
install -m 644 "${APP_DIR}/deploy/okx-trade.service" /etc/systemd/system/okx-trade.service
install -m 644 "${APP_DIR}/deploy/okx-trade-healthcheck.service" /etc/systemd/system/okx-trade-healthcheck.service
install -m 644 "${APP_DIR}/deploy/okx-trade-healthcheck.timer" /etc/systemd/system/okx-trade-healthcheck.timer
systemctl daemon-reload

step "[6/6] Done"
cat <<EOF

Next steps:
  1) Edit creds:           sudo -u ${APP_USER} nano ${APP_DIR}/.env
  2) Smoke test --check:   sudo -u ${APP_USER} ${VENV}/bin/python ${APP_DIR}/scripts/live.py --check
  3) Enable + start:       sudo systemctl enable --now okx-trade
                           sudo systemctl enable --now okx-trade-healthcheck.timer
  4) Watch logs:           journalctl -u okx-trade -f
  5) Status:               systemctl status okx-trade okx-trade-healthcheck.timer

EOF
