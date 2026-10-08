#!/data/data/com.termux/files/usr/bin/bash
# telegent on an Android phone (Termux), one-time setup. In Termux:
#   pkg install -y git && git clone <repo-url> && bash telegent/termux/setup.sh
# Installs python + cloudflared + sshd, lets the admin PC in by key (termux/admin.pub),
# registers autostart for Termux:Boot and prints the phone's address.
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "== пакеты (несколько минут)"
export DEBIAN_FRONTEND=noninteractive
yes | pkg upgrade -y -o Dpkg::Options::=--force-confnew >/dev/null 2>&1 || true
pkg install -y python openssh cloudflared termux-tools >/dev/null

echo "== ssh для администрирования с ПК (только по ключу, порт 8022)"
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
while IFS= read -r key; do
  case "$key" in ""|"#"*) continue;; esac
  ! grep -qF "$key" ~/.ssh/authorized_keys && echo "$key" >> ~/.ssh/authorized_keys
done < "$REPO/termux/admin.pub"
grep -q '^PasswordAuthentication no' "$PREFIX/etc/ssh/sshd_config" 2>/dev/null \
  || echo 'PasswordAuthentication no' >> "$PREFIX/etc/ssh/sshd_config"
pgrep -x sshd >/dev/null || sshd

echo "== автозапуск: при каждом открытии Termux и (если стоит Termux:Boot) после перезагрузки"
touch ~/.bashrc
grep -q 'pgrep -x sshd' ~/.bashrc || cat >> ~/.bashrc <<'EOF'
# telegent: admin ssh for the PC (key only)
pgrep -x sshd >/dev/null || sshd
EOF
grep -q 'telegent/termux/start.sh' ~/.bashrc || cat >> ~/.bashrc <<EOF
# telegent: start the server + tunnel if not running yet
(nohup bash "$REPO/termux/start.sh" >/dev/null 2>&1 &)
EOF
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/telegent <<EOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
sshd
exec bash "$REPO/termux/start.sh"
EOF
chmod +x ~/.termux/boot/telegent

termux-wake-lock || true
(nohup bash "$REPO/termux/start.sh" >/dev/null 2>&1 &)
IP=$(python -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print(s.getsockname()[0])" 2>/dev/null || echo "?")
echo
echo "ГОТОВО. Адрес телефона в Wi-Fi: $IP (ssh -p 8022 <user>@$IP)"
