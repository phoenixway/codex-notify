# Codex Notify v1.0.0

Локальна система сповіщень для Fedora / Hyprland / WezTerm / Codex CLI / KDE Connect.
Версія зафіксована 2026-11-01. Залежності Python: лише стандартна бібліотека, Python 3.11+.

Політика: Away після 120 секунд → KDE Connect; активна панель цього Codex → тиша;
інший/невідомий фокус → notify-send normal. Недоступний телефон → desktop fallback.
Телефон не використовується без підтвердженого стану Away. Повний текст відповіді,
промпти та повні шляхи не надсилаються: лише факт завершення й останній компонент cwd.
Quiet policy Ambxst залишається чинною. Код Ambxst і політики живлення не змінюються.

## Збереження

Цей репозиторій містить лише код, інсталятор і тести. Особисті конфігурації,
журнали, ключі й runtime state не включені. ID телефона передається інсталятору.

## Відтворення

Спочатку налаштуйте Hyprland, WezTerm, Codex CLI та pairing KDE Connect. Потрібні
python3, hypridle, hyprctl, wezterm, kdeconnect-cli, notify-send та systemctl.
Інсталятор нічого не завантажує й не встановлює пакети.

```sh
rtk proxy git clone https://github.com/phoenixway/codex-notify.git
cd codex-notify
rtk proxy git checkout v1.0.0
rtk proxy python3 test_notify.py
rtk proxy python3 test_install.py
rtk proxy kdeconnect-cli --list-devices
rtk proxy python3 install.py --device-id YOUR_DEVICE_ID
```

Якщо RTK після перевстановлення ще немає, запускайте ті самі команди без `rtk proxy`.

За замовчуванням інсталятор використовує наявний hyprland-session.target. Він має
існувати й запускатися вашою конфігурацією Hyprland, як у поточному середовищі.
Перед інсталяцією перевірте способи запуску та відсутність другого Hypridle:

```sh
rtk proxy systemctl --user status hyprland-session.target
rtk proxy systemctl --user cat hypridle.service
rtk proxy pgrep -a hypridle
```

Для іншого перевіреного способу запуску задайте --target graphical-session.target.
Не створюйте другий autostart. Якщо вже є hypridle.conf з іншими правилами,
інсталятор відмовиться його замінювати: тоді потрібно вручну об'єднати activity
listener зі збереженням існуючих політик та врахувати supervisor.

Шляхи формуються з HOME. Для нового ID пристрою:

```sh
rtk proxy kdeconnect-cli --list-devices
rtk proxy python3 install.py --device-id YOUR_DEVICE_ID
```

Перед першою активацією переконайтеся, що systemd має актуальні змінні поточного
Hyprland-сеансу (у старому середовищі це вже робить startup):

```sh
rtk proxy systemctl --user import-environment WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE
rtk proxy systemctl --user daemon-reload
rtk proxy systemctl --user start hypridle.service
rtk proxy systemctl --user status hypridle.service
```

Інсталятор зберігає оригінали перед записом у приватний install-backups/UUID,
додає notify на верхньому рівні й notifications=false в наявний [tui], зберігає
інші TOML-значення. Складне непідтримуване форматування або інший notify → відмова
до запису. Він створює symlink autostart, але сам не запускає/зупиняє служби.
Новий запуск Codex читає зміни; поточний інтерактивний сеанс не перезапускайте.

## Перевірка та діагностика

```sh
rtk proxy python3 ~/.local/bin/codex-notify.py --version
rtk proxy python3 ~/.local/bin/codex-notify.py --dry-run '{"type":"agent-turn-complete","cwd":"/tmp/demo"}'
rtk proxy tail -n 20 ~/.local/state/codex-notify/diagnostic.jsonl
```

Для фактичного тесту запустіть у панелі Codex:

```sh
rtk proxy python3 - <<'PY'
import json, pathlib, subprocess, time, uuid
payload = {"type":"agent-turn-complete", "thread-id":"manual-test",
           "turn-id":str(uuid.uuid4()), "cwd":str(pathlib.Path.cwd())}
time.sleep(8)
subprocess.run(["python3", str(pathlib.Path.home()/".local/bin/codex-notify.py"), json.dumps(payload)])
PY
```

У тій самій панелі → тиша. Перемкніть вкладку/панель/застосунок протягом 8 секунд → desktop.
Для Away замініть 8 на 135 й не користуйтесь клавіатурою/мишею.
У журналі outcome: silenced, desktop_sent, phone_submitted або send_failed.
phone_submitted означає успішне завершення локальної KDE Connect команди;
це не підтвердження доставки або прочитання на телефоні. desktop_sent означає
успіх notify-send; показ залежить від поточної Quiet policy.

Дедуплікація: thread-id + turn-id на 24 години, до 4096 подій. Без turn-id — hash
payload на 2 секунди: ідентичні швидкі завершення старого клієнта можуть об'єднатися.
Timeout: фокус/доступність по 1.5 с, ping 3 с, desktop 2 с, lock до 1 с.
Стан Away перевіряє boot ID, сеанс, PID/starttime supervisor/Hypridle і token.
Недоступність спостерігача не спричиняє повідомлень на телефон.
Кілька вікон із неоднозначними назвами дають desktop normal, щоб уникнути хибної тиші.

## Rollback нового встановлення

Підставте шлях backup, надрукований інсталятором:

```sh
rtk proxy systemctl --user stop hypridle.service
rtk proxy python3 install.py --rollback ~/.local/share/codex-notify/install-backups/UUID
rtk proxy systemctl --user daemon-reload
```

Rollback відновлює файли та symlink попереднього стану. Якщо їх змінено після
інсталяції, він відмовляється перезаписувати: об'єднайте зміни з manifest.json вручну.
Не вмикає й не запускає попередні служби. 

## Джерела та наступна версія

- https://learn.chatgpt.com/docs/config-file/config-advanced#notifications
- https://wezterm.org/cli/cli/list-clients.html
- https://wiki.hypr.land/0.56.0/Hypr-Ecosystem/hypridle/

У v1.0 немає черги/автоматичного retry. Наступне покращення: коротка черга з TTL,
дедуплікацією та повторною перевіркою Away перед кожною спробою; скасовувати
телефонну доставку після повернення користувача за ПК.
