#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="SanBot"
INSTALL_DIR="${SANBOT_HOME:-/opt/sanbot}"
SANBOT_IMAGE_DEFAULT="${SANBOT_IMAGE:-ghcr.io/sanshanhyo/sanbot:latest}"
NAPCAT_IMAGE_DEFAULT="${NAPCAT_IMAGE:-mlikiowa/napcat-docker:latest}"
NAPCAT_WEBUI_PORT_DEFAULT="${NAPCAT_WEBUI_PORT:-6099}"

CURRENT_STEP="启动安装器"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

on_error() {
  local line="${1:-unknown}"
  printf '\n[ERROR] %s 安装失败，位置：第 %s 行，当前步骤：%s\n' "$APP_NAME" "$line" "$CURRENT_STEP" >&2
  printf '你可以修复问题后重新运行脚本；已生成的配置不会自动删除。\n' >&2
}
trap 'on_error "$LINENO"' ERR

print_banner() {
  cat <<'BANNER'
  ____              ____        _
 / ___|  __ _ _ __ | __ )  ___ | |_
 \___ \ / _` | '_ \|  _ \ / _ \| __|
  ___) | (_| | | | | |_) | (_) | |_
 |____/ \__,_|_| |_|____/ \___/ \__|

BANNER
}

print_success_cat() {
  cat <<'CAT'
        ／＞　 フ
       | 　_　_|      z Z
      ／ ミ＿xノ
     /　　　　 |
    /　 ヽ　　 ﾉ
   │　　|　|　|
／￣|　　 |　|　|
|　 |　　 |　|　|
ヽ＿ヽ＿_ヽ_)__)
CAT
}

progress() {
  local percent="$1"
  local message="$2"
  local width=32
  local filled=$((percent * width / 100))
  local empty=$((width - filled))
  local fill_bar empty_bar

  fill_bar="$(printf '%*s' "$filled" '' | tr ' ' '#')"
  empty_bar="$(printf '%*s' "$empty" '' | tr ' ' '-')"
  printf '[%3d%%] [%s%s] %s\n' "$percent" "$fill_bar" "$empty_bar" "$message"
}

die() {
  printf '[ERROR] %s\n' "$1" >&2
  exit 1
}

warn() {
  printf '[WARN] %s\n' "$1" >&2
}

need_tty() {
  if [ ! -r /dev/tty ]; then
    die "安装需要交互式终端。请在 SSH 终端中运行：curl -fsSL <install.sh 地址> | sudo bash"
  fi
}

ask() {
  local prompt="$1"
  local default="${2:-}"
  local value

  if [ -n "$default" ]; then
    printf '%s [%s]: ' "$prompt" "$default" > /dev/tty
  else
    printf '%s: ' "$prompt" > /dev/tty
  fi

  IFS= read -r value < /dev/tty || true
  if [ -z "$value" ]; then
    value="$default"
  fi
  printf '%s' "$value"
}

ask_secret() {
  local prompt="$1"
  local default="${2:-}"
  local value

  if [ -n "$default" ]; then
    printf '%s [%s]: ' "$prompt" "$default" > /dev/tty
  else
    printf '%s: ' "$prompt" > /dev/tty
  fi

  IFS= read -r -s value < /dev/tty || true
  printf '\n' > /dev/tty
  if [ -z "$value" ]; then
    value="$default"
  fi
  printf '%s' "$value"
}

ask_yes_no() {
  local prompt="$1"
  local default="${2:-N}"
  local suffix answer normalized

  if [ "$default" = "Y" ]; then
    suffix='[Y/n]'
  else
    suffix='[y/N]'
  fi

  while true; do
    printf '%s %s: ' "$prompt" "$suffix" > /dev/tty
    IFS= read -r answer < /dev/tty || true
    normalized="$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')"
    case "$normalized" in
      "")
        if [ "$default" = "Y" ]; then
          return 0
        fi
        return 1
        ;;
      y|yes)
        return 0
        ;;
      n|no)
        return 1
        ;;
      *)
        printf '请输入 Y 或 N。\n' > /dev/tty
        ;;
    esac
  done
}

ask_numeric_required() {
  local prompt="$1"
  local value
  while true; do
    value="$(ask "$prompt")"
    if [[ "$value" =~ ^[0-9]+$ ]]; then
      printf '%s' "$value"
      return
    fi
    printf '请输入纯数字。\n' > /dev/tty
  done
}

ask_numeric_default() {
  local prompt="$1"
  local default="$2"
  local value
  while true; do
    value="$(ask "$prompt" "$default")"
    if [[ "$value" =~ ^[0-9]+$ ]]; then
      printf '%s' "$value"
      return
    fi
    printf '请输入纯数字。\n' > /dev/tty
  done
}

random_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
    return
  fi
  od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
}

strip_newlines() {
  local value="$1"
  value="${value//$'\r'/}"
  value="${value//$'\n'/}"
  printf '%s' "$value"
}

escape_yaml_double() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\r'/}"
  value="${value//$'\n'/}"
  printf '%s' "$value"
}

normalize_avs_cookie() {
  local value="$1"
  value="$(strip_newlines "$value")"
  if [[ "$value" == *"AVS="* ]]; then
    value="${value#*AVS=}"
    value="${value%%;*}"
  fi
  printf '%s' "$value"
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "请使用 root 或 sudo 运行安装脚本，例如：curl -fsSL <install.sh 地址> | sudo bash"
  fi
}

require_linux() {
  if [ "$(uname -s)" != "Linux" ]; then
    die "一键部署脚本仅支持 Linux 服务器。Windows 本地测试请继续使用虚拟环境方式启动。"
  fi
}

show_agreement() {
  cat <<'AGREEMENT'
安装前请确认你理解并同意：
1. SanBot 是基于 OneBot 11 协议通信，故您不该违反 OneBot 11 所涉及条约内容。
2. 本脚本可能会安装 Docker、拉取容器镜像、创建 /opt/sanbot 目录并设置开机自启。
3. 请勿将 NapCat HTTP/WebSocket 端口暴露到公网。
4. 请勿将 QQ 登录信息、Cookie、Token 泄露给他人。
5. 使用者需自行确保使用行为符合当地法律法规、平台规则和内容版权要求。
AGREEMENT
}

install_docker() {
  CURRENT_STEP="检查/安装 Docker"
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    progress 24 "Docker 与 Compose 已存在，跳过安装"
  else
    progress 18 "正在安装 Docker 与 Compose 插件"
    local tmp_script
    tmp_script="$(mktemp)"
    curl -fsSL https://get.docker.com -o "$tmp_script"
    sh "$tmp_script"
    rm -f "$tmp_script"
  fi

  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now docker >/dev/null 2>&1 || warn "无法通过 systemctl 启动 Docker，请手动检查 docker 服务。"
  fi

  docker info >/dev/null 2>&1 || die "Docker daemon 未运行。请启动 Docker 后重新执行脚本。"
  docker compose version >/dev/null 2>&1 || die "Docker Compose 插件不可用。"
}

backup_if_exists() {
  local file="$1"
  if [ -f "$file" ]; then
    cp "$file" "${file}.bak.${TIMESTAMP}"
  fi
}

write_env_line() {
  local file="$1"
  local key="$2"
  local value="$3"
  printf '%s=%s\n' "$key" "$(strip_newlines "$value")" >> "$file"
}

write_env_file() {
  local env_file="$INSTALL_DIR/.env"
  local backend_token="$1"
  local napcat_access_token="$2"
  local enable_search="$3"
  local webui_bind="$4"
  local bot_qq="$5"
  local bot_name="$6"
  local manager_name="$7"
  local manager_qq="$8"
  local max_jobs="$9"
  local image_threads="${10}"
  local photo_threads="${11}"

  backup_if_exists "$env_file"
  : > "$env_file"

  write_env_line "$env_file" "SANBOT_IMAGE" "$SANBOT_IMAGE_DEFAULT"
  write_env_line "$env_file" "NAPCAT_IMAGE" "$NAPCAT_IMAGE_DEFAULT"
  write_env_line "$env_file" "NAPCAT_UID" "$(id -u)"
  write_env_line "$env_file" "NAPCAT_GID" "$(id -g)"
  write_env_line "$env_file" "NAPCAT_WEBUI_BIND" "$webui_bind"
  write_env_line "$env_file" "NAPCAT_WEBUI_PORT" "$NAPCAT_WEBUI_PORT_DEFAULT"

  write_env_line "$env_file" "BOT_QQ_ID" "$bot_qq"
  write_env_line "$env_file" "BOT_LANG" "zh_CN"
  write_env_line "$env_file" "BOT_DISPLAY_NAME" "$bot_name"
  write_env_line "$env_file" "BOT_MANAGER_NAME" "$manager_name"
  write_env_line "$env_file" "BOT_MANAGER_QQ" "$manager_qq"
  write_env_line "$env_file" "BOT_MANAGER_QQ_IDS" "$manager_qq"

  write_env_line "$env_file" "NAPCAT_WS_URL" "ws://napcat:3001"
  write_env_line "$env_file" "NAPCAT_HTTP_URL" "http://napcat:3000"
  write_env_line "$env_file" "NAPCAT_ACCESS_TOKEN" "$napcat_access_token"
  write_env_line "$env_file" "NAPCAT_HTTP_TIMEOUT_SECONDS" "60"
  write_env_line "$env_file" "NAPCAT_UPLOAD_TIMEOUT_SECONDS" "900"
  write_env_line "$env_file" "NAPCAT_MAX_UPLOAD_BYTES" "104857600"
  write_env_line "$env_file" "NAPCAT_MAX_UPLOAD_FILENAME_BYTES" "96"
  write_env_line "$env_file" "NAPCAT_UPLOAD_RETRIES" "5"

  write_env_line "$env_file" "BACKEND_URL" "http://backend:8000"
  write_env_line "$env_file" "BACKEND_API_TOKEN" "$backend_token"
  write_env_line "$env_file" "BACKEND_HOST" "0.0.0.0"
  write_env_line "$env_file" "BACKEND_PORT" "8000"

  write_env_line "$env_file" "ENABLE_SEARCH" "$enable_search"
  write_env_line "$env_file" "SEARCH_TIMEOUT_SECONDS" "20"
  write_env_line "$env_file" "SEARCH_RESULT_LIMIT" "5"
  write_env_line "$env_file" "SEARCH_CONFIRM_TIMEOUT_SECONDS" "600"

  write_env_line "$env_file" "MAX_CONCURRENT_JOBS" "$max_jobs"
  write_env_line "$env_file" "MAX_ACTIVE_JOBS_PER_GROUP" "3"
  write_env_line "$env_file" "MAX_ACTIVE_JOBS_PER_USER" "1"
  write_env_line "$env_file" "JOB_TIMEOUT_SECONDS" "1800"
  write_env_line "$env_file" "JOB_STALL_TIMEOUT_SECONDS" "300"
  write_env_line "$env_file" "JOB_PROGRESS_CHECK_SECONDS" "10"
  write_env_line "$env_file" "JOB_PROGRESS_NOTIFY_SECONDS" "300"
  write_env_line "$env_file" "JOB_CONFIRM_TIMEOUT_SECONDS" "600"
  write_env_line "$env_file" "USER_COMMAND_COOLDOWN_SECONDS" "10"
  write_env_line "$env_file" "LARGE_ALBUM_WARNING_PAGES" "100"

  write_env_line "$env_file" "CACHE_CLEANUP_INTERVAL_SECONDS" "3600"
  write_env_line "$env_file" "JOB_CACHE_TTL_SECONDS" "259200"
  write_env_line "$env_file" "BOT_DOWNLOAD_CACHE_TTL_SECONDS" "259200"
  write_env_line "$env_file" "PREVIEW_CACHE_TTL_SECONDS" "86400"

  write_env_line "$env_file" "JM_DOWNLOAD_IMAGE_THREADS" "$image_threads"
  write_env_line "$env_file" "JM_DOWNLOAD_PHOTO_THREADS" "$photo_threads"
  write_env_line "$env_file" "JM_DOWNLOAD_MAX_IMAGE_THREADS" "$image_threads"
  write_env_line "$env_file" "JM_DOWNLOAD_MAX_PHOTO_THREADS" "$photo_threads"
  write_env_line "$env_file" "DATA_DIR" "/app/data"
  write_env_line "$env_file" "JMCOMIC_OPTION_PATH" "/app/config/jmcomic-option.yml"
  write_env_line "$env_file" "LOG_LEVEL" "INFO"

  chmod 600 "$env_file"
}

write_jmcomic_option() {
  local cookie="$1"
  local image_threads="$2"
  local photo_threads="$3"
  local config_file="$INSTALL_DIR/config/jmcomic-option.yml"
  local escaped_cookie
  escaped_cookie="$(escape_yaml_double "$cookie")"

  backup_if_exists "$config_file"
  cat > "$config_file" <<YAML
client:
  impl: api
  retry_times: 5
  postman:
    meta_data:
      headers:
        User-Agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
      cookies:
        AVS: "${escaped_cookie}"

download:
  image:
    decode: true
  threading:
    image: ${image_threads}
    photo: ${photo_threads}
YAML
  chmod 600 "$config_file"
}

write_compose_file() {
  local compose_file="$INSTALL_DIR/docker-compose.yml"
  backup_if_exists "$compose_file"
  cat > "$compose_file" <<'YAML'
services:
  backend:
    image: ${SANBOT_IMAGE}
    container_name: sanbot-backend
    restart: unless-stopped
    init: true
    command: ["python", "-m", "backend.main"]
    env_file:
      - .env
    environment:
      BACKEND_HOST: 0.0.0.0
      BACKEND_URL: http://backend:8000
      DATA_DIR: /app/data
      JMCOMIC_OPTION_PATH: /app/config/jmcomic-option.yml
    volumes:
      - ./data:/app/data
      - ./config:/app/config:ro
    ports:
      - "127.0.0.1:${BACKEND_PORT:-8000}:8000"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]
      interval: 15s
      timeout: 5s
      retries: 20
      start_period: 20s

  bot:
    image: ${SANBOT_IMAGE}
    container_name: sanbot-bot
    restart: unless-stopped
    init: true
    command: ["python", "-m", "bot.main"]
    depends_on:
      backend:
        condition: service_healthy
      napcat:
        condition: service_started
    env_file:
      - .env
    environment:
      BACKEND_URL: http://backend:8000
      NAPCAT_HTTP_URL: http://napcat:3000
      NAPCAT_WS_URL: ws://napcat:3001
      DATA_DIR: /app/data
      JMCOMIC_OPTION_PATH: /app/config/jmcomic-option.yml
    volumes:
      - ./data:/app/data
      - ./config:/app/config:ro

  napcat:
    image: ${NAPCAT_IMAGE}
    container_name: sanbot-napcat
    restart: unless-stopped
    init: true
    environment:
      NAPCAT_UID: ${NAPCAT_UID:-1000}
      NAPCAT_GID: ${NAPCAT_GID:-1000}
    ports:
      - "${NAPCAT_WEBUI_BIND:-127.0.0.1}:${NAPCAT_WEBUI_PORT:-6099}:6099"
      - "127.0.0.1:3000:3000"
      - "127.0.0.1:3001:3001"
    volumes:
      - ./napcat/QQ:/app/.config/QQ
      - ./napcat/config:/app/napcat/config
      - ./napcat/plugins:/app/napcat/plugins
      - ./data:/app/data
YAML
}

write_manager_command() {
  cat > /usr/local/bin/sanbot <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${SANBOT_HOME:-/opt/sanbot}"
COMPOSE=(docker compose -f "$APP_DIR/docker-compose.yml" --env-file "$APP_DIR/.env")

usage() {
  cat <<'USAGE'
SanBot 管理命令：
  sanbot status             查看容器状态
  sanbot logs [服务名]       查看日志，可选服务名：backend / bot / napcat
  sanbot restart            重启全部服务
  sanbot stop               停止服务
  sanbot start              启动服务
  sanbot update             拉取最新镜像并重启
  sanbot doctor             做一次基础连通性检查
  sanbot uninstall          停止并移除容器，不删除 /opt/sanbot/data
USAGE
}

ensure_app() {
  if [ ! -f "$APP_DIR/docker-compose.yml" ]; then
    echo "未找到 $APP_DIR/docker-compose.yml，请先安装 SanBot。" >&2
    exit 1
  fi
}

env_value() {
  local key="$1"
  local file="$APP_DIR/.env"
  [ -f "$file" ] || return 0
  grep -E "^${key}=" "$file" | tail -n 1 | cut -d= -f2-
}

case "${1:-help}" in
  status)
    ensure_app
    "${COMPOSE[@]}" ps
    ;;
  logs)
    ensure_app
    shift || true
    "${COMPOSE[@]}" logs -f --tail=200 "$@"
    ;;
  restart)
    ensure_app
    "${COMPOSE[@]}" restart
    ;;
  stop)
    ensure_app
    "${COMPOSE[@]}" stop
    ;;
  start)
    ensure_app
    "${COMPOSE[@]}" up -d
    ;;
  update)
    ensure_app
    "${COMPOSE[@]}" pull
    "${COMPOSE[@]}" up -d
    ;;
  doctor)
    ensure_app
    echo "[1/4] Docker daemon"
    docker info >/dev/null && echo "OK"
    echo "[2/4] Compose config"
    "${COMPOSE[@]}" config >/dev/null && echo "OK"
    echo "[3/4] Containers"
    "${COMPOSE[@]}" ps
    echo "[4/4] Backend health"
    port="$(env_value BACKEND_PORT)"
    port="${port:-8000}"
    if command -v curl >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:${port}/health"; then
      echo
      echo "OK"
    else
      echo "Backend health check failed. Use: sanbot logs backend" >&2
      exit 1
    fi
    ;;
  uninstall)
    ensure_app
    read -r -p "确认停止并移除 SanBot 容器？/opt/sanbot/data 不会删除 [y/N]: " answer
    case "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" in
      y|yes)
        "${COMPOSE[@]}" down
        echo "容器已移除，数据仍在 $APP_DIR/data。"
        ;;
      *)
        echo "已取消。"
        ;;
    esac
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage
    exit 1
    ;;
esac
SH
  chmod 755 /usr/local/bin/sanbot
}

public_ip() {
  curl -fsS --max-time 3 https://api.ipify.org 2>/dev/null || true
}

main() {
  need_tty
  require_linux
  require_root

  print_banner
  show_agreement
  if ! ask_yes_no "是否同意以上内容并继续安装？" "Y"; then
    die "已取消安装。"
  fi

  CURRENT_STEP="收集配置"
  progress 8 "正在收集 SanBot 基础配置"
  local bot_qq bot_name manager_name manager_qq avs_cookie enable_search webui_bind
  local max_jobs image_threads photo_threads backend_token napcat_access_token

  bot_qq="$(ask_numeric_required "请输入机器人 QQ 号")"
  bot_name="$(ask "请输入机器人显示名称" "SanBot")"
  manager_name="$(ask "请输入管理者名称" "管理者")"
  manager_qq="$(ask_numeric_required "请输入管理者 QQ 号")"
  avs_cookie="$(normalize_avs_cookie "$(ask_secret "请输入 JMComic AVS Cookie（可先留空，稍后修改 /opt/sanbot/config/jmcomic-option.yml）")")"

  if ask_yes_no "是否默认启用搜索功能？" "Y"; then
    enable_search="true"
  else
    enable_search="false"
  fi

  max_jobs="$(ask_numeric_default "最大同时下载任务数" "1")"
  image_threads="$(ask_numeric_default "JM 图片下载线程数" "16")"
  photo_threads="$(ask_numeric_default "JM 分册下载线程数" "4")"

  if ask_yes_no "是否临时将 NapCat WebUI 绑定到公网 0.0.0.0？完成扫码后建议改回 127.0.0.1" "N"; then
    webui_bind="0.0.0.0"
  else
    webui_bind="127.0.0.1"
  fi

  backend_token="$(random_token)"
  napcat_access_token=""

  install_docker

  CURRENT_STEP="创建安装目录"
  progress 34 "正在创建 $INSTALL_DIR"
  if [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
    warn "检测到已有 SanBot 安装目录。脚本会备份 .env、docker-compose.yml 与 jmcomic-option.yml，不会删除 data。"
    if ! ask_yes_no "是否继续并重新生成配置？" "N"; then
      die "已取消安装。"
    fi
  fi
  install -d -m 755 "$INSTALL_DIR" "$INSTALL_DIR/config" "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
  install -d -m 755 "$INSTALL_DIR/napcat/QQ" "$INSTALL_DIR/napcat/config" "$INSTALL_DIR/napcat/plugins"

  CURRENT_STEP="生成配置文件"
  progress 48 "正在写入 .env 与 JMComic 配置"
  write_env_file "$backend_token" "$napcat_access_token" "$enable_search" "$webui_bind" "$bot_qq" "$bot_name" "$manager_name" "$manager_qq" "$max_jobs" "$image_threads" "$photo_threads"
  write_jmcomic_option "$avs_cookie" "$image_threads" "$photo_threads"

  CURRENT_STEP="生成 Docker Compose"
  progress 60 "正在写入 docker-compose.yml"
  write_compose_file

  CURRENT_STEP="安装管理命令"
  progress 70 "正在安装 sanbot 管理命令"
  write_manager_command

  CURRENT_STEP="拉取镜像"
  progress 78 "正在拉取 SanBot 与 NapCat 镜像"
  (cd "$INSTALL_DIR" && docker compose pull)

  CURRENT_STEP="启动服务"
  progress 90 "正在启动服务并设置开机自启"
  (cd "$INSTALL_DIR" && docker compose up -d)

  CURRENT_STEP="完成安装"
  progress 100 "SanBot 已部署完成"
  print_success_cat

  local ip webui_url
  ip="$(public_ip)"
  if [ -z "$ip" ]; then
    ip="<服务器IP>"
  fi
  if [ "$webui_bind" = "0.0.0.0" ]; then
    webui_url="http://${ip}:${NAPCAT_WEBUI_PORT_DEFAULT}"
  else
    webui_url="http://127.0.0.1:${NAPCAT_WEBUI_PORT_DEFAULT}（需要 SSH 隧道访问）"
  fi

  cat <<EOF

安装目录：$INSTALL_DIR
管理命令：
  sanbot status
  sanbot logs bot
  sanbot logs napcat
  sanbot doctor

下一步：
1. 打开 NapCat WebUI：$webui_url
2. 登录机器人 QQ，并在 NapCat 中启用 OneBot 11 HTTP 与 WebSocket。
3. HTTP 地址使用 0.0.0.0:3000，WebSocket 地址使用 0.0.0.0:3001；不要把 3000/3001 暴露到公网。
4. 修改 JMComic Cookie：$INSTALL_DIR/config/jmcomic-option.yml
5. 查看机器人日志：sanbot logs bot

如果你把 WebUI 临时开放到了公网，完成扫码和配置后建议把 $INSTALL_DIR/.env 中的 NAPCAT_WEBUI_BIND 改回 127.0.0.1，然后执行：
  sanbot restart
EOF
}

main "$@"
