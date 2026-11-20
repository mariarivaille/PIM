# Деплой Pim на свой сервер

Сервер: Ubuntu 26.04, от 1 vCPU и 1 ГБ RAM (голосовые распознаёт Gemini через OpenRouter; для локального
Whisper, `STT_PROVIDER=whisper`, нужно 4 ГБ), от 10 ГБ диска, публичный IPv4. Начальная настройка — `cloud-init.yaml` (вставить свой публичный ключ
вместо `SSH_PUBLIC_KEY`).

## Если cloud-init не применился

Например, ключ добавлен в панели хостера, а user-data не вставлен: тогда вход есть только под `root`.
Те же шаги из `cloud-init.yaml` выполняются вручную, в таком порядке, чтобы не потерять доступ:
пользователь `admin` с ключом и sudo → проверить вход под ним → только потом закрыть вход под root и по паролю
(`/etc/ssh/sshd_config.d/00-pim.conf`) → ufw, swap, unattended-upgrades → uv, клон, `.venv`, сервис, бэкап.
Папка `/opt/pim` закрыта для всех, кроме `pim`: команды внутри — через `sudo -u pim -H sh -c "cd /opt/pim/app && …"`.

## После создания сервера

```sh
# cloud-init работает пару минут после старта; дождаться:
ssh admin@IP 'cloud-init status --wait'

# перенести секреты и базу (бот на ноутбуке перед этим остановить — два бота с одним токеном конфликтуют)
scp .env admin@IP:/tmp/pim.env
scp pim.db admin@IP:/tmp/pim.db
ssh admin@IP 'sudo install -o pim -g pim -m 600 /tmp/pim.env /opt/pim/app/.env &&
              sudo install -o pim -g pim -m 600 /tmp/pim.db /opt/pim/app/pim.db &&
              rm /tmp/pim.env /tmp/pim.db && sudo systemctl start pim'

ssh admin@IP 'journalctl -u pim -f'   # логи
```

## Обновление

```sh
ssh admin@IP 'sudo -u pim -H sh -c "cd /opt/pim/app && git pull -q &&
              uv pip install -q --python .venv/bin/python -e ." && sudo systemctl restart pim'
```

## Резервные копии

Каждую ночь `/usr/local/bin/pim-backup` кладёт копию базы в `/var/backups/pim` (14 дней). Это тот же диск —
от удаления сервера не спасает, поэтому время от времени забирать к себе:

```sh
scp 'admin@IP:/var/backups/pim/pim-*.db' ~/pim-backups/
```

## HTTPS для Mini App

Telegram открывает Mini App только по HTTPS. Caddy сам получает и продлевает сертификат, нужен домен; без своего
подойдёт `IP-С-ДЕФИСАМИ.sslip.io`:

```sh
sudo apt-get install -y caddy
echo '203-0-113-5.sslip.io {
    reverse_proxy 127.0.0.1:8080
}' | sudo tee /etc/caddy/Caddyfile && sudo systemctl reload caddy
# в /opt/pim/app/.env: WEBAPP_URL=https://203-0-113-5.sslip.io — и перезапустить pim
```
