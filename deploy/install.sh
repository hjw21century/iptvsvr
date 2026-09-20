#!/usr/bin/env bash
# 把 IPTV-Hub 绑定到 iptv.tomeleaf.com：装 systemd 单元 + nginx 站点 + 签发证书。
# 幂等，可重复执行。需要 root（本机 / 是只读挂载时请在宿主/可写环境下执行）。
set -euo pipefail

DOMAIN="${DOMAIN:-iptv.tomeleaf.com}"
SRC="${SRC:-/data/code/iptv}"
PORT="${PORT:-8088}"

[[ $EUID -eq 0 ]] || { echo "请用 root 执行"; exit 1; }

say() { printf "\n\033[1;34m==> %s\033[0m\n" "$*"; }

say "1/5 安装并启动后端服务"
install -m 644 "$SRC/deploy/iptv-hub.service" /etc/systemd/system/iptv-hub.service
systemctl daemon-reload
# 停掉手工用 nohup 起的实例，避免和 systemd 抢端口
pkill -f "^python3 -m iptvhub serve" 2>/dev/null || true
sleep 1
systemctl enable --now iptv-hub
for i in $(seq 1 15); do
    curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
    sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || {
    echo "后端未就绪，请看 journalctl -u iptv-hub -n 50"; exit 1; }
echo "后端 OK: 127.0.0.1:$PORT"

say "2/5 安装 nginx 站点（HTTP，用于 ACME 校验）"
install -m 644 "$SRC/deploy/nginx/iptv.http.conf" /etc/nginx/conf.d/iptv.conf
nginx -t
systemctl reload nginx
curl -fsS -o /dev/null "http://$DOMAIN/healthz" && echo "HTTP 可达: http://$DOMAIN"

say "3/5 签发 TLS 证书"
if [[ -d "/etc/letsencrypt/live/$DOMAIN" ]]; then
    echo "证书已存在，跳过签发"
else
    certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos --keep-until-expiring
fi

say "4/5 切换到 HTTPS 站点配置"
install -m 644 "$SRC/deploy/nginx/iptv.tls.conf" /etc/nginx/conf.d/iptv.conf
nginx -t
systemctl reload nginx

say "5/5 自检"
curl -fsS -o /dev/null -w "  https://$DOMAIN/            -> %{http_code}\n" "https://$DOMAIN/"
curl -fsS -o /dev/null -w "  https://$DOMAIN/healthz     -> %{http_code}\n" "https://$DOMAIN/healthz"
curl -fsS -o /dev/null -w "  https://$DOMAIN/playlist.m3u -> %{http_code}\n" "https://$DOMAIN/playlist.m3u"
curl -fsS -o /dev/null -w "  http  跳转                  -> %{http_code}\n" "http://$DOMAIN/" || true

cat <<TIP

完成。订阅地址：
  https://$DOMAIN/playlist.m3u
  https://$DOMAIN/playlist.m3u?group=央视频道&min_height=1080&backups=1

常用命令：
  systemctl status iptv-hub            查看服务
  journalctl -u iptv-hub -f            看日志（含每 4 小时的自动更新）
  curl -X POST http://127.0.0.1:$PORT/api/update   手动触发一次更新
TIP
