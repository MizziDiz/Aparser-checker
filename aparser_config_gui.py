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
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from lib.config_schema import CONFIG_FIELDS, CONFIG_FILENAME, coerce, load_values, save_values


def base_dir() -> Path:
    """Каталог, где лежит config.json: рядом с .exe (frozen) или со скриптом."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = base_dir() / CONFIG_FILENAME


class ConfigApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("A-Parser monitor — настройки")
        self.geometry("640x720")
        self.vars: dict[str, tk.Variable] = {}
        self._build()
        self._load()

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
        ttk.Button(bar, text="Выход", command=self.destroy).pack(side="right")
        self.status = ttk.Label(self, text="", foreground="#2a7")
        self.status.pack(fill="x", padx=10, pady=(0, 8))

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

    def _test_telegram(self):
        # собираем полноценный cfg (с приведением типов) и шлём тест
        cfg = {f["key"]: coerce(f, self.vars[f["key"]].get()) for f in CONFIG_FIELDS}
        cfg.setdefault("request_timeout", 30)
        try:
            from aparser_monitor import send_telegram
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Нет модуля", f"Не удалось импортировать отправку: {e}")
            return
        ok = send_telegram(cfg, "✅ aparser_monitor: проверка из настроек")
        if ok:
            messagebox.showinfo("Telegram", "Сообщение отправлено — проверьте чат.")
        else:
            messagebox.showerror("Telegram", "Не отправлено. Проверьте токен/chat_id/прокси/релей "
                                             "(подробности — в консоли/логе).")


if __name__ == "__main__":
    ConfigApp().mainloop()
