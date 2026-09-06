#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Точный клик по времени — Pro (Precise Time-Synced Multi-Click)
------------------------------------------------------------------
v2: добавлены тёмная/светлая тема и автокликер по НЕСКОЛЬКИМ позициям
(как на ПК, так и на подключённом по ADB Android-устройстве).

Синхронизируется с реальным сетевым временем через NTP (тот же принцип,
на котором работает time.is) и кликает по заданным координатам экрана
ровно в указанный момент — с точностью, ограниченной только вашей ОС
(на практике — единицы-десятки миллисекунд).

Зависимости:
    pip install pynput
    pip install sv-ttk      # необязательно, но даёт красивую тему (иначе — свой fallback)

Запуск:
    python precise_clicker.py
"""

import socket
import struct
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

try:
    from pynput import keyboard
    from pynput.mouse import Button
    from pynput.mouse import Controller as MouseController
except ImportError as e:
    raise SystemExit(
        "Не найдена библиотека pynput.\nУстановите её: pip install pynput"
    ) from e

try:
    import sv_ttk  # красивая современная тема с готовой поддержкой dark/light
    HAS_SV_TTK = True
except ImportError:
    HAS_SV_TTK = False


NTP_SERVERS = ["pool.ntp.org", "time.google.com", "time.cloudflare.com"]
NTP_DELTA = 2208988800  # разница между эпохой NTP (1900) и эпохой Unix (1970)


def get_ntp_offset(server: str, timeout: float = 3.0):
    """
    Возвращает (offset, t3), где:
      offset — расхождение между реальным (сетевым) временем и часами
               этого компьютера, в секундах (истинное - локальное);
      t3     — локальное время (time.time()) в момент получения ответа.

    Используется стандартный алгоритм SNTP с компенсацией сетевой
    задержки — тот же принцип, что лежит в основе time.is и системных
    часов Windows/macOS/Android.
    """
    packet = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t0 = time.time()
        s.sendto(packet, (server, 123))
        data, _ = s.recvfrom(48)
        t3 = time.time()

    unpacked = struct.unpack("!12I", data)
    t1 = unpacked[8] + float(unpacked[9]) / 2**32 - NTP_DELTA
    t2 = unpacked[10] + float(unpacked[11]) / 2**32 - NTP_DELTA

    offset = ((t1 - t0) + (t2 - t3)) / 2.0
    return offset, t3


# ----------------------------------------------------------------------
# Тема оформления
# ----------------------------------------------------------------------

_FALLBACK_DARK = dict(bg="#1e1e1e", fg="#eaeaea", field="#2b2b2b", accent="#3a3a3a")
_FALLBACK_LIGHT = dict(bg="#f4f4f4", fg="#1a1a1a", field="#ffffff", accent="#e2e2e2")


def apply_fallback_theme(root: tk.Tk, dark: bool):
    """Простая тёмная/светлая тема на случай, если sv_ttk не установлен."""
    palette = _FALLBACK_DARK if dark else _FALLBACK_LIGHT
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=palette["bg"])
    for name in (
        "TFrame", "TLabelframe", "TLabelframe.Label", "TLabel",
        "TNotebook", "TCheckbutton",
    ):
        style.configure(name, background=palette["bg"], foreground=palette["fg"])
    style.configure(
        "TNotebook.Tab", background=palette["accent"], foreground=palette["fg"]
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", palette["bg"])],
        foreground=[("selected", palette["fg"])],
    )
    style.configure("TButton", background=palette["accent"], foreground=palette["fg"])
    style.map("TButton", background=[("active", palette["field"])])
    style.configure(
        "TEntry", fieldbackground=palette["field"], foreground=palette["fg"]
    )
    style.configure(
        "Treeview",
        background=palette["field"],
        foreground=palette["fg"],
        fieldbackground=palette["field"],
    )
    style.configure(
        "Treeview.Heading", background=palette["accent"], foreground=palette["fg"]
    )
    style.map("Treeview", background=[("selected", "#3B82F6")])


# ----------------------------------------------------------------------
# Небольшой диалог "введите X, Y вручную"
# ----------------------------------------------------------------------

def ask_xy(parent, title="Добавить позицию", init_x=0, init_y=0):
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.resizable(False, False)
    dlg.transient(parent.winfo_toplevel())
    dlg.grab_set()

    result = {}

    ttk.Label(dlg, text="X:").grid(row=0, column=0, padx=8, pady=8, sticky="e")
    var_x = tk.StringVar(value=str(init_x))
    ttk.Entry(dlg, textvariable=var_x, width=10).grid(row=0, column=1, padx=8, pady=8)

    ttk.Label(dlg, text="Y:").grid(row=1, column=0, padx=8, pady=(0, 8), sticky="e")
    var_y = tk.StringVar(value=str(init_y))
    ttk.Entry(dlg, textvariable=var_y, width=10).grid(row=1, column=1, padx=8, pady=(0, 8))

    def on_ok(event=None):
        try:
            result["value"] = (int(var_x.get()), int(var_y.get()))
            dlg.destroy()
        except ValueError:
            messagebox.showerror("Ошибка", "X и Y должны быть целыми числами", parent=dlg)

    def on_cancel():
        dlg.destroy()

    btns = ttk.Frame(dlg)
    btns.grid(row=2, column=0, columnspan=2, pady=(0, 10))
    ttk.Button(btns, text="OK", command=on_ok).pack(side="left", padx=4)
    ttk.Button(btns, text="Отмена", command=on_cancel).pack(side="left", padx=4)

    dlg.bind("<Return>", on_ok)
    dlg.wait_window()
    return result.get("value")


# ----------------------------------------------------------------------
# Переиспользуемый виджет: список позиций (X, Y) с управлением
# ----------------------------------------------------------------------

class PositionListFrame(ttk.Frame):
    def __init__(self, parent, hint_text: str = None):
        super().__init__(parent)

        columns = ("idx", "x", "y")
        self.tree = ttk.Treeview(
            self, columns=columns, show="headings", height=6, selectmode="browse"
        )
        self.tree.heading("idx", text="#")
        self.tree.heading("x", text="X")
        self.tree.heading("y", text="Y")
        self.tree.column("idx", width=36, anchor="center")
        self.tree.column("x", width=90, anchor="center")
        self.tree.column("y", width=90, anchor="center")
        self.tree.grid(row=0, column=0, rowspan=6, sticky="nsew", padx=(0, 8))

        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        ttk.Button(self, text="➕ Вручную", command=self.add_manual).grid(
            row=0, column=1, sticky="ew", pady=2
        )
        ttk.Button(self, text="🗑 Удалить", command=self.remove_selected).grid(
            row=1, column=1, sticky="ew", pady=2
        )
        ttk.Button(self, text="⬆ Вверх", command=self.move_up).grid(
            row=2, column=1, sticky="ew", pady=2
        )
        ttk.Button(self, text="⬇ Вниз", command=self.move_down).grid(
            row=3, column=1, sticky="ew", pady=2
        )
        ttk.Button(self, text="✖ Очистить", command=self.clear).grid(
            row=4, column=1, sticky="ew", pady=2
        )

        if hint_text:
            ttk.Label(self, text=hint_text, wraplength=140, foreground="#888").grid(
                row=5, column=1, sticky="new", pady=(6, 0)
            )

    def _reindex(self):
        for i, item in enumerate(self.tree.get_children(), start=1):
            _, x, y = self.tree.item(item, "values")
            self.tree.item(item, values=(i, x, y))

    def add_position(self, x, y):
        idx = len(self.tree.get_children()) + 1
        self.tree.insert("", "end", values=(idx, x, y))

    def add_manual(self):
        result = ask_xy(self)
        if result:
            self.add_position(*result)

    def remove_selected(self):
        for item in self.tree.selection():
            self.tree.delete(item)
        self._reindex()

    def clear(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

    def move_up(self):
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        idx = self.tree.index(item)
        if idx > 0:
            self.tree.move(item, "", idx - 1)
            self._reindex()

    def move_down(self):
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        idx = self.tree.index(item)
        self.tree.move(item, "", idx + 1)
        self._reindex()

    def get_positions(self):
        positions = []
        for item in self.tree.get_children():
            _, x, y = self.tree.item(item, "values")
            positions.append((int(x), int(y)))
        return positions

    def __len__(self):
        return len(self.tree.get_children())


# ----------------------------------------------------------------------
# Основное приложение
# ----------------------------------------------------------------------

class ClickerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Точный клик по времени — Pro")
        root.geometry("580x760")
        root.minsize(540, 680)

        self.dark_mode = tk.BooleanVar(value=True)
        self.offset_true_anchor = None
        self.offset_perf_anchor = None
        self.stop_flag = threading.Event()
        self.mouse = MouseController()

        self._build_ui()
        self.apply_theme()

        self.kb_listener = keyboard.Listener(on_press=self.on_key)
        self.kb_listener.start()

        self.update_clock()

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=12, pady=(12, 4))
        ttk.Label(
            top, text="Точный клик по времени", font=("Segoe UI", 14, "bold")
        ).pack(side="left")
        self.btn_theme = ttk.Button(top, text="", command=self.toggle_theme)
        self.btn_theme.pack(side="right")

        sync_frame = ttk.LabelFrame(self.root, text="Синхронизация времени (NTP)")
        sync_frame.pack(fill="x", padx=12, pady=4)
        row = ttk.Frame(sync_frame)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Button(row, text="Синхронизировать", command=self.do_sync).pack(side="left")
        self.lbl_sync_status = ttk.Label(row, text="Время не синхронизировано")
        self.lbl_sync_status.pack(side="left", padx=10)
        self.lbl_now = ttk.Label(sync_frame, text="—", font=("Consolas", 20))
        self.lbl_now.pack(anchor="w", padx=8, pady=(0, 8))

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=12, pady=4)

        # --- Вкладка: позиции ПК ---
        tab_pc = ttk.Frame(notebook)
        notebook.add(tab_pc, text="Позиции (ПК)")
        ttk.Label(
            tab_pc,
            text="Наведите курсор на нужную точку и нажмите F9 — позиция добавится в список\n"
                 "(работает даже если окно свёрнуто). Порядок в списке = порядок кликов.",
            foreground="#888",
        ).pack(anchor="w", padx=8, pady=(8, 4))
        self.pc_positions = PositionListFrame(tab_pc, hint_text="F9 — захват\nкурсора")
        self.pc_positions.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # --- Вкладка: Android через ADB ---
        tab_android = ttk.Frame(notebook)
        notebook.add(tab_android, text="Android (ADB)")
        self.var_use_adb = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            tab_android,
            text="Также тапать по Android (нужен телефон, подключённый по USB,\nс включённой отладкой по USB)",
            variable=self.var_use_adb,
        ).pack(anchor="w", padx=8, pady=(8, 4))
        self.android_positions = PositionListFrame(tab_android)
        self.android_positions.pack(fill="both", expand=True, padx=8, pady=4)
        row_off = ttk.Frame(tab_android)
        row_off.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(row_off, text="Компенсация задержки ADB (мс):").pack(side="left")
        self.var_adb_offset = tk.IntVar(value=60)
        ttk.Entry(row_off, textvariable=self.var_adb_offset, width=6).pack(
            side="left", padx=6
        )

        # --- Вкладка: расписание ---
        tab_sched = ttk.Frame(notebook)
        notebook.add(tab_sched, text="Расписание")

        frm_time = ttk.LabelFrame(tab_sched, text="Время первого клика (24ч, локальное время ПК)")
        frm_time.pack(fill="x", padx=8, pady=8)
        row2 = ttk.Frame(frm_time)
        row2.pack(fill="x", padx=8, pady=8)
        now = datetime.now()
        self.var_h = tk.StringVar(value=now.strftime("%H"))
        self.var_m = tk.StringVar(value=now.strftime("%M"))
        self.var_s = tk.StringVar(value="00")
        self.var_ms = tk.StringVar(value="000")
        for lbl, var, w in [
            ("Час", self.var_h, 3),
            ("Мин", self.var_m, 3),
            ("Сек", self.var_s, 3),
            ("Мс", self.var_ms, 4),
        ]:
            ttk.Label(row2, text=lbl).pack(side="left")
            ttk.Entry(row2, textvariable=var, width=w).pack(side="left", padx=(2, 10))

        frm_multi = ttk.LabelFrame(tab_sched, text="Последовательность кликов")
        frm_multi.pack(fill="x", padx=8, pady=8)
        row3 = ttk.Frame(frm_multi)
        row3.pack(fill="x", padx=8, pady=8)
        ttk.Label(row3, text="Всего шагов:").pack(side="left")
        self.var_count = tk.IntVar(value=1)
        ttk.Entry(row3, textvariable=self.var_count, width=5).pack(side="left", padx=4)
        ttk.Button(
            row3, text="= кол-ву позиций", command=self.sync_count_to_positions
        ).pack(side="left", padx=(4, 14))
        ttk.Label(row3, text="Интервал между шагами (мс):").pack(side="left")
        self.var_interval = tk.IntVar(value=200)
        ttk.Entry(row3, textvariable=self.var_interval, width=6).pack(side="left", padx=4)
        ttk.Label(
            tab_sched,
            text="Если позиций несколько — клики идут по списку по порядку и зацикливаются,\n"
                 "если шагов больше, чем позиций в списке.",
            foreground="#888",
        ).pack(anchor="w", padx=8, pady=(0, 8))

        # --- Низ: запуск/отмена ---
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=12, pady=(4, 6))
        self.btn_arm = ttk.Button(bottom, text="ВЗВЕСТИ (запустить ожидание)", command=self.arm)
        self.btn_arm.pack(fill="x", pady=(0, 4))
        self.btn_cancel = ttk.Button(bottom, text="Отмена", command=self.cancel, state="disabled")
        self.btn_cancel.pack(fill="x")

        self.lbl_status = ttk.Label(self.root, text="", wraplength=540, justify="left")
        self.lbl_status.pack(padx=12, pady=(0, 12), anchor="w")

    def sync_count_to_positions(self):
        n = max(len(self.pc_positions), len(self.android_positions), 1)
        self.var_count.set(n)

    # ---------- Тема ----------
    def toggle_theme(self):
        self.dark_mode.set(not self.dark_mode.get())
        self.apply_theme()

    def apply_theme(self):
        dark = self.dark_mode.get()
        if HAS_SV_TTK:
            sv_ttk.set_theme("dark" if dark else "light")
        else:
            apply_fallback_theme(self.root, dark)
        self.btn_theme.config(text="☀️ Светлая тема" if dark else "🌙 Тёмная тема")

    # ---------- Время ----------
    def do_sync(self):
        self.lbl_sync_status.config(text="Синхронизация...")
        self.root.update_idletasks()
        last_err = None
        for server in NTP_SERVERS:
            try:
                offset, t_local = get_ntp_offset(server)
                self.offset_true_anchor = t_local + offset
                self.offset_perf_anchor = time.perf_counter()
                self.lbl_sync_status.config(
                    text=f"OK ({server}), поправка: {offset * 1000:+.1f} мс"
                )
                return
            except Exception as e:
                last_err = e
        self.lbl_sync_status.config(text=f"Ошибка синхронизации: {last_err}")

    def true_time(self) -> float:
        if self.offset_perf_anchor is None:
            return time.time()
        return self.offset_true_anchor + (time.perf_counter() - self.offset_perf_anchor)

    def update_clock(self):
        t = self.true_time()
        self.lbl_now.config(text=datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3])
        self.root.after(50, self.update_clock)

    # ---------- Захват координат курсора ----------
    def on_key(self, key):
        try:
            if key == keyboard.Key.f9:
                x, y = self.mouse.position
                self.root.after(0, lambda: self._capture_pc_position(x, y))
        except Exception:
            pass

    def _capture_pc_position(self, x, y):
        self.pc_positions.add_position(x, y)
        self.lbl_status.config(text=f"Добавлена позиция ПК: {x}, {y}")

    # ---------- Запуск ----------
    def arm(self):
        try:
            h, m, s = int(self.var_h.get()), int(self.var_m.get()), int(self.var_s.get())
            ms = int(self.var_ms.get())
            count = max(1, self.var_count.get())
            interval = max(0, self.var_interval.get()) / 1000.0

            pc_positions = self.pc_positions.get_positions()
            android_positions = self.android_positions.get_positions()
            use_adb = self.var_use_adb.get()
            adb_offset = self.var_adb_offset.get() / 1000.0

            if not pc_positions and not (use_adb and android_positions):
                messagebox.showwarning(
                    "Нет позиций",
                    "Добавьте хотя бы одну позицию клика (F9 на вкладке «Позиции (ПК)»\n"
                    "или вручную на вкладке «Android (ADB)»).",
                )
                return

            now_dt = datetime.fromtimestamp(self.true_time())
            target_dt = now_dt.replace(hour=h, minute=m, second=s, microsecond=ms * 1000)
            if target_dt <= now_dt:
                target_dt = datetime.fromtimestamp(target_dt.timestamp() + 86400)
            target_epoch = target_dt.timestamp()
        except (ValueError, tk.TclError):
            messagebox.showerror(
                "Ошибка", "Проверьте введённые значения (время / количество / интервал)"
            )
            return

        self.stop_flag.clear()
        self.btn_arm.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.lbl_status.config(
            text=f"Ожидание... цель: {target_dt.strftime('%H:%M:%S.%f')[:-3]}"
        )

        threading.Thread(
            target=self.wait_and_click,
            args=(target_epoch, pc_positions, android_positions, count, interval, use_adb, adb_offset),
            daemon=True,
        ).start()

    def cancel(self):
        self.stop_flag.set()
        self.btn_arm.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self.lbl_status.config(text="Отменено")

    def wait_until(self, t_target: float) -> bool:
        while True:
            if self.stop_flag.is_set():
                return False
            remaining = t_target - self.true_time()
            if remaining <= 0:
                return True
            if remaining > 0.05:
                time.sleep(min(remaining - 0.02, 0.2))
            # последние ~20-50 мс — активное ожидание (busy-wait) для максимальной точности

    def send_adb_tap(self, x: int, y: int):
        try:
            subprocess.run(
                ["adb", "shell", "input", "tap", str(x), str(y)],
                timeout=2,
                capture_output=True,
            )
        except Exception as e:
            print("ADB ошибка:", e)

    def wait_and_click(
        self, target_epoch, pc_positions, android_positions, count, interval, use_adb, adb_offset
    ):
        n_pc = len(pc_positions)
        n_android = len(android_positions)

        for i in range(count):
            if self.stop_flag.is_set():
                break
            t_click = target_epoch + i * interval

            if use_adb and n_android:
                ax, ay = android_positions[i % n_android]
                if not self.wait_until(t_click - adb_offset):
                    break
                threading.Thread(
                    target=self.send_adb_tap, args=(ax, ay), daemon=True
                ).start()

            if not self.wait_until(t_click):
                break

            if n_pc:
                x, y = pc_positions[i % n_pc]
                fire_time = self.true_time()
                self.mouse.position = (x, y)
                self.mouse.click(Button.left)
                delta_ms = (fire_time - t_click) * 1000
                msg = f"Клик {i + 1}/{count} @ ({x},{y}): расхождение {delta_ms:+.1f} мс"
            else:
                msg = f"Шаг {i + 1}/{count} (только Android)"
            self.root.after(0, lambda m=msg: self.lbl_status.config(text=m))

        self.root.after(0, lambda: self.btn_arm.config(state="normal"))
        self.root.after(0, lambda: self.btn_cancel.config(state="disabled"))


if __name__ == "__main__":
    root = tk.Tk()
    app = ClickerApp(root)
    root.mainloop()
