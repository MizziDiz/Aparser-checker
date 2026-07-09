#!/usr/bin/env python3
"""
aparser_config_gui.py — локальный редактор конфигурации aparser_monitor (Tkinter).

Рисует форму по схеме aparser_config_schema.CONFIG_FIELDS, читает/сохраняет
aparser_monitor.config.json рядом с собой (или рядом с .exe, если собран PyInstaller).
Кнопка «Проверить Telegram» шлёт тестовое сообщение с текущими значениями формы.

Запуск: python aparser_config_gui.py
Сборка в .exe: pyinstaller --onefile --windowed --name aparser-config aparser_config_gui.py
"""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from lib.config_schema import CONFIG_FIELDS, CONFIG_FILENAME, coerce, load_values, save_values


def base_dir() -> Path:
    """Каталог, где лежит config.json: рядом с .exe (frozen) или со скриптом."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


DATA_DIR = base_dir() / "data"
CONFIG_PATH = DATA_DIR / CONFIG_FILENAME


def ensure_data_dir() -> None:
    """Создаёт data/ и переносит туда конфиг из старого расположения (корень)."""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        legacy = base_dir() / CONFIG_FILENAME
        if legacy.exists() and not CONFIG_PATH.exists():
            legacy.replace(CONFIG_PATH)
    except OSError:
        pass


class ConfigApp(tk.Tk):
    def __init__(self):
        super().__init__()
        ensure_data_dir()
        self.title("A-Parser monitor — настройки")
        self.geometry("640x760")
        self.vars: dict[str, tk.Variable] = {}
        self._relay_srv = None          # запущенный сервер-релей (если стартовали из GUI)
        self._relay_thread = None
        self._build()
        self._load()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # — построение формы —
    def _build(self):
        top = ttk.Label(self, text=f"Файл: {CONFIG_PATH}", foreground="#555")
        top.pack(fill="x", padx=10, pady=(8, 0))

        # прокручиваемая область
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True, padx=10, pady=8)
        canvas = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.form = ttk.Frame(canvas)
        self.form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.form, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        last_group = None
        for f in CONFIG_FIELDS:
            if f["group"] != last_group:
                last_group = f["group"]
                ttk.Label(self.form, text=f["group"], font=("", 10, "bold")
                          ).grid(sticky="w", pady=(12, 2), columnspan=2)
            row = self.form.grid_size()[1]
            ttk.Label(self.form, text=f["label"]).grid(row=row, column=0, sticky="w", padx=(4, 8))
            if f["type"] == "bool":
                var = tk.BooleanVar()
                ttk.Checkbutton(self.form, variable=var).grid(row=row, column=1, sticky="w")
            else:
                var = tk.StringVar()
                show = "*" if f["type"] == "password" else ""
                ttk.Entry(self.form, textvariable=var, width=44, show=show
                          ).grid(row=row, column=1, sticky="we")
            self.vars[f["key"]] = var
            if f.get("help"):
                ttk.Label(self.form, text=f["help"], foreground="#888"
                          ).grid(row=row + 1, column=1, sticky="w", pady=(0, 4))

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=8)
        ttk.Button(bar, text="Сохранить", command=self._save).pack(side="left")
        ttk.Button(bar, text="Проверить Telegram", command=self._test_telegram).pack(side="left", padx=8)
        ttk.Button(bar, text="Проверка пересылки", command=self._test_forward).pack(side="left")
        ttk.Button(bar, text="Выход", command=self._on_close).pack(side="right")
        self.status = ttk.Label(self, text="", foreground="#2a7")
        self.status.pack(fill="x", padx=10, pady=(0, 4))

        # — управление релеем: ТОЛЬКО на сервере-релее, не на клиентах —
        relay = ttk.LabelFrame(self, text="Релей — запускать ТОЛЬКО на сервере-релее (не на клиентах)")
        relay.pack(fill="x", padx=10, pady=(0, 10))
        self._btn_start = ttk.Button(relay, text="Запуск реле", command=self._relay_start)
        self._btn_start.pack(side="left", padx=(6, 4), pady=6)
        self._btn_stop = ttk.Button(relay, text="Остановка реле", command=self._relay_stop, state="disabled")
        self._btn_stop.pack(side="left", padx=4, pady=6)
        self.relay_status = ttk.Label(relay, text="релей: остановлен", foreground="#888")
        self.relay_status.pack(side="left", padx=10)

        # — автозапуск и планировщик задач (Windows) —
        auto = ttk.LabelFrame(self, text="Автозапуск и планировщик (Windows)")
        auto.pack(fill="x", padx=10, pady=(0, 10))
        r1 = ttk.Frame(auto); r1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(r1, text="python.exe:").pack(side="left")
        self.py_var = tk.StringVar(value=self._detect_python())
        ttk.Entry(r1, textvariable=self.py_var, width=52).pack(side="left", padx=6, fill="x", expand=True)
        r2 = ttk.Frame(auto); r2.pack(fill="x", padx=6, pady=2)
        ttk.Label(r2, text="Интервал монитора, мин:").pack(side="left")
        self.interval_var = tk.StringVar(value="5")
        ttk.Entry(r2, textvariable=self.interval_var, width=6).pack(side="left", padx=6)
        r3 = ttk.Frame(auto); r3.pack(fill="x", padx=6, pady=(4, 8))
        ttk.Button(r3, text="Bat автозапуска релея", command=self._make_relay_bat).pack(side="left")
        ttk.Button(r3, text="Задача: релей при старте", command=self._task_relay).pack(side="left", padx=6)
        ttk.Button(r3, text="Задача: монитор каждые N мин", command=self._task_monitor).pack(side="left")

    # — данные —
    def _load(self):
        values = load_values(CONFIG_PATH)
        for f in CONFIG_FIELDS:
            v = values.get(f["key"], f["default"])
            if f["type"] == "bool":
                self.vars[f["key"]].set(bool(v))
            else:
                self.vars[f["key"]].set("" if v is None else str(v))

    def _form_dict(self) -> dict:
        return {f["key"]: self.vars[f["key"]].get() for f in CONFIG_FIELDS}

    def _save(self):
        try:
            save_values(CONFIG_PATH, self._form_dict())
        except (ValueError, OSError) as e:
            messagebox.showerror("Ошибка сохранения", str(e))
            return
        self.status.config(text=f"Сохранено: {CONFIG_PATH}", foreground="#2a7")

    def _cfg(self) -> dict:
        """Текущие значения формы как cfg (с приведением типов)."""
        cfg = {f["key"]: coerce(f, self.vars[f["key"]].get()) for f in CONFIG_FIELDS}
        cfg.setdefault("request_timeout", 30)
        return cfg

    def _test_telegram(self):
        try:
            from aparser_monitor import send_telegram
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Нет модуля", f"Не удалось импортировать отправку: {e}")
            return
        ok = send_telegram(self._cfg(), "✅ aparser_monitor: проверка из настроек")
        if ok:
            messagebox.showinfo("Telegram", "Сообщение отправлено — проверьте чат.")
        else:
            messagebox.showerror("Telegram", "Не отправлено. Проверьте токен/chat_id/прокси/релей "
                                             "(подробности — в консоли/логе).")

    # — проверка файловой пересылки: запись на шару (autosend_dest) + уведомление —
    def _test_forward(self):
        import time
        cfg = self._cfg()
        dest = cfg.get("autosend_dest", "")
        if not dest:
            messagebox.showwarning("Проверка пересылки",
                                   "Заполните «Назначение (UNC-шара)» (autosend_dest) — "
                                   "файлы результатов пересылаются туда.")
            return
        p = Path(dest)
        testfile = p / f"aparser_monitor_test_{int(time.time())}.txt"

        def notify(text):
            try:
                from aparser_monitor import send_telegram
                send_telegram(cfg, text)
            except Exception:  # noqa: BLE001
                pass

        try:
            p.mkdir(parents=True, exist_ok=True)
            testfile.write_text("aparser_monitor: проверка доступа к шаре", encoding="utf-8")
            testfile.unlink()
        except OSError as e:
            notify(f"⚠️ <b>Проверка пересылки на шару НЕ удалась</b>\n{dest}\n{e}")
            messagebox.showerror("Проверка пересылки",
                                 f"Нет доступа к шаре:\n{dest}\n{e}\n\n"
                                 f"Проверьте путь и права пользователя (для UNC — доступ к сети).")
            return
        notify(f"📤 <b>Проверка пересылки: доступ к шаре есть</b>\n{dest}")
        messagebox.showinfo("Проверка пересылки",
                            f"Запись на шару прошла успешно (тестовый файл удалён).\n{dest}\n"
                            f"Уведомление отправлено в Telegram.")

    # — запуск/остановка релея (только на сервере-релее) —
    def _relay_start(self):
        if self._relay_srv is not None:
            return
        cfg = self._cfg()
        try:
            from aparser_monitor import get_logger
            from lib.relay import build_relay
            srv = build_relay(cfg, get_logger())
        except OSError as e:
            messagebox.showerror("Запуск релея",
                                 f"Не удалось занять порт {cfg.get('relay_port')}:\n{e}\n"
                                 f"Возможно, релей уже запущен (служба/задача).")
            return
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Запуск релея", str(e))
            return
        self._relay_srv = srv
        self._relay_thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self._relay_thread.start()
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")
        self.relay_status.config(
            text=f"релей: запущен на {cfg.get('relay_bind')}:{cfg.get('relay_port')}", foreground="#2a7")

    def _relay_stop(self):
        if self._relay_srv is None:
            return
        try:
            self._relay_srv.shutdown()
            self._relay_srv.server_close()
        except Exception:  # noqa: BLE001
            pass
        self._relay_srv = None
        self._relay_thread = None
        self._btn_start.config(state="normal")
        self._btn_stop.config(state="disabled")
        self.relay_status.config(text="релей: остановлен", foreground="#888")

    # — автозапуск и планировщик —
    @staticmethod
    def _detect_python() -> str:
        import shutil
        for name in ("python.exe", "python", "python3"):
            found = shutil.which(name)
            if found:
                return found
        return "python"

    def _make_relay_bat(self) -> Path | None:
        base = base_dir()
        py = self.py_var.get().strip() or "python"
        bat = base / "relay-run.bat"
        content = (
            "@echo off\r\n"
            f'cd /d "{base}"\r\n'
            ":loop\r\n"
            f'"{py}" aparser_monitor_ui.py --relay\r\n'
            "timeout /t 5 /nobreak >nul\r\n"
            "goto loop\r\n"
        )
        try:
            bat.write_text(content, encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Bat автозапуска", str(e))
            return None
        self.status.config(text=f"Создан {bat}", foreground="#2a7")
        return bat

    def _run_schtasks(self, cmd: str, label: str) -> None:
        import subprocess
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Планировщик", str(e))
            return
        if r.returncode == 0:
            messagebox.showinfo("Планировщик", f"Задача создана: {label}.")
        else:
            out = (r.stderr or r.stdout or b"").decode("cp866", "replace").strip()
            messagebox.showerror(
                "Планировщик",
                f"Не удалось создать задачу «{label}» (код {r.returncode}).\n{out}\n\n"
                f"Обычно нужен запуск редактора «от имени администратора». Команда:\n{cmd}")

    def _task_relay(self):
        bat = self._make_relay_bat()
        if bat is None:
            return
        cmd = (f'schtasks /Create /TN "aparser-relay" /SC ONSTART /RU SYSTEM '
               f'/RL HIGHEST /F /TR "{bat}"')
        self._run_schtasks(cmd, "релей (автозапуск при старте, от SYSTEM)")

    def _task_monitor(self):
        base = base_dir()
        py = self.py_var.get().strip() or "python"
        try:
            n = max(1, int(float(self.interval_var.get())))
        except ValueError:
            n = 5
        script = base / "aparser_monitor_ui.py"
        # /TR с путём к exe в кавычках: внутренние кавычки экранируются \"
        cmd = (f'schtasks /Create /TN "aparser-monitor-ui" /SC MINUTE /MO {n} '
               f'/RL HIGHEST /F /TR "\\"{py}\\" \\"{script}\\""')
        self._run_schtasks(cmd, f"монитор каждые {n} мин (от текущего пользователя)")

    def _on_close(self):
        self._relay_stop()
        self.destroy()


if __name__ == "__main__":
    ConfigApp().mainloop()
