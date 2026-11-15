# Деплой Pim на свой сервер

Сервер: Ubuntu 26.04, от 2 vCPU и 4 ГБ RAM (локальный Whisper large-v3-turbo занимает ~1.5 ГБ),
от 20 ГБ диска, публичный IPv4. Начальная настройка — `cloud-init.yaml` (вставить свой публичный ключ
вместо `SSH_PUBLIC_KEY`).

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
ssh admin@IP 'cd /opt/pim/app && sudo -u pim git pull &&
              sudo -u pim -H uv pip install --python .venv/bin/python -e . && sudo systemctl restart pim'
```

## Резервные копии

Каждую ночь `/usr/local/bin/pim-backup` кладёт копию базы в `/var/backups/pim` (14 дней). Это тот же диск —
от удаления сервера не спасает, поэтому время от времени забирать к себе:

```sh
scp 'admin@IP:/var/backups/pim/pim-*.db' ~/pim-backups/
```

## HTTPS для Mini App

Telegram открывает Mini App только по HTTPS. Caddy получает сертификат сам, нужен домен. Без своего домена
подойдёт `IP-С-ДЕФИСАМИ.sslip.io` (например, `203-0-113-5.sslip.io`). Caddyfile появится вместе с API.
