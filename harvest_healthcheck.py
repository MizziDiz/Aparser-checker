#!/usr/bin/env python3
"""Проверка узла .14 и харвеста — раз в 4 часа (cron на .50), с Telegram-сводкой/алертом.

Проверяет: доступность web-UI A-Parser (порт 9092), «свежесть» харвест-раундов,
прирост доменов за 4 часа, диск .50 и .14, размер harvest.db, прокси-пул. Шлёт
Telegram: 🔴 если UI лёг или раунды застряли, иначе 🟢 краткий дайджест.
"""
from __future__ import annotations
import json, re, shutil, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import aparser_monitor as am

OUT = REPO / "data" / "harvest"
# инфра узла — из gitignored data/nodes/harvest_node.json (как в harvest_runner);
# шаблон-плейсхолдеры в harvest_node.example.json; пересоздать — recreate-creds.sh
_NODE_CFG = REPO / "data" / "nodes" / "harvest_node.json"
NODE = json.loads(_NODE_CFG.read_text(encoding="utf-8")) if _NODE_CFG.exists() else \
    {"smb": "//<LAN-IP>/C$", "creds": "/root/.smbcreds<N>", "ui_url": "http://<LAN-IP>:9092/"}
_m = re.search(r"\.(\d+)/", NODE["smb"])
NID = _m.group(1) if _m else "0"                 # номер узла, напр. "14"
UI = NODE["ui_url"]
# дефолты монитора + node-конфиг (телеграм и пр.; сырой JSON без request_timeout → KeyError в send_telegram)
CFG = {**am.DEFAULTS, **json.loads((REPO / "data" / "nodes" / f"node-{NID}.config.json").read_text())}


def ui_up() -> bool:
    try:
        urllib.request.urlopen(UI, timeout=15)
        return True
    except urllib.error.HTTPError:
        return True                     # 401/403 = сервер отвечает (нужна авторизация) = ЖИВ
    except Exception:
        return False                    # connection refused/timeout = лёг


def last_round_age_min() -> float | None:
    """Минуты с последнего успешного раунда (по mtime harvest_log.csv)."""
    csv = OUT / "harvest_log.csv"
    if not csv.exists():
        return None
    return (time.time() - csv.stat().st_mtime) / 60


def new_domains_4h() -> int:
    csv = OUT / "harvest_log.csv"
    if not csv.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=4)
    total = 0
    for line in csv.read_text(encoding="utf-8", errors="ignore").splitlines()[1:]:
        p = line.split(",")
        if len(p) < 7:
            continue
        try:
            if datetime.fromisoformat(p[0]) >= cutoff:
                total += int(p[6])
        except Exception:
            continue
    return total


def node14_free_gb() -> float | None:
    try:
        out = subprocess.run(["smbclient", NODE["smb"], "-A", NODE["creds"],
                              "-c", "du"], capture_output=True, text=True, timeout=40).stdout
        m = re.search(r"(\d+) blocks of size (\d+)\. (\d+) available", out)
        if m:
            return int(m.group(3)) * int(m.group(2)) / 2**30
    except Exception:
        pass
    return None


def _count(path: Path) -> int:
    return sum(1 for _ in path.open(encoding="utf-8", errors="ignore")) if path.exists() else 0


def main() -> None:
    up = ui_up()
    age = last_round_age_min()
    alerts = []
    L = []

    if up:
        L.append(f"🟢 <b>.{NID} A-Parser</b>: жив (9092 отвечает)")
    else:
        L.append(f"🔴 <b>.{NID} A-Parser НЕ отвечает</b> (порт 9092 refused)")
        alerts.append("UI down")

    if age is None:
        L.append("харвест: нет лога")
    else:
        stale = age > 25
        L.append(f"{'⚠️' if stale else '•'} последний раунд: {age:.0f} мин назад")
        if stale:
            alerts.append(f"раунды застряли ({age:.0f}м)")

    L.append(f"• прирост доменов за 4ч: <b>{new_domains_4h()}</b>")
    L.append(f"• цели: {_count(OUT/'targets.txt')} | пул сидов: {_count(OUT/'seeds_expanded.txt')}")

    du = shutil.disk_usage("/")
    free50 = du.free / 2**30
    L.append(f"• диск .50: {free50:.0f} ГБ своб")
    if free50 < 15:
        alerts.append(f"диск .50 мало ({free50:.0f}ГБ)")
    f14 = node14_free_gb()
    if f14 is not None:
        L.append(f"• диск .14: {f14:.0f} ГБ своб")
        if f14 < 15:
            alerts.append(f"диск .14 мало ({f14:.0f}ГБ)")

    db = OUT / "harvest.db"
    if db.exists():
        L.append(f"• harvest.db: {db.stat().st_size/2**20:.0f} МБ")

    head = f"🚨 <b>Проверка .{NID} — ПРОБЛЕМА</b>\n" if alerts else f"🩺 <b>Проверка .{NID} — ОК</b>\n"
    if alerts:
        head += "❗ " + "; ".join(alerts) + "\n"
    msg = head + "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", ""))
    try:
        am.send_telegram(CFG, msg)
    except Exception as e:
        print("Telegram не отправлен:", e)


if __name__ == "__main__":
    main()
