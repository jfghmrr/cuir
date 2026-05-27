from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext

import config

# Тёмная палитра — подстраиваемся под macOS Dark Mode (системный Tk 8.5 принудительно
# темнит фон tk.* виджетов и игнорирует светлые цвета).
BG = "#1e1e1e"
FG = "#e8e8e8"
MUTED = "#999999"
ENTRY_BG = "#2b2b2b"
BTN_BG = "#3a3a3a"
ACCENT = "#4a90e2"

from playwright.sync_api import sync_playwright

import auth
import bars
import parser as excel_parser


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("БАРС: автозаполнение ДЗ")
        self.geometry("820x620")
        # Системный Tk 8.5 на macOS не даёт ttk-виджетам видимый фон в тёмной теме —
        # используем классические tk.* виджеты с явно заданными цветами.
        self.configure(bg=BG)

        self.file_var = tk.StringVar()
        self.klass_var = tk.StringVar()
        self.subject_var = tk.StringVar()
        self.group_var = tk.StringVar()
        self.period_var = tk.StringVar(value="2 Полугодие")
        self.dry_run_var = tk.BooleanVar(value=True)

        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.confirm_event = threading.Event()
        self.finish_event = threading.Event()
        self.worker: threading.Thread | None = None

        self.log_file_path = config.PROJECT_ROOT / "bars.log"
        try:
            with open(self.log_file_path, "a", encoding="utf-8") as f:
                f.write(f"\n=== Запуск {datetime.now().isoformat(timespec='seconds')} ===\n")
        except Exception:
            pass

        self._build_ui()
        self.after(100, self._drain_log)
        # При закрытии окна — снимаем все wait'ы и даём worker'у завершиться,
        # чтобы Chromium-процесс закрылся штатно (а не остался зомби).
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        self.confirm_event.set()
        self.finish_event.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=5.0)
        self.destroy()

    # ───────────── UI ─────────────

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        def lbl(parent, text):
            return tk.Label(parent, text=text, bg=BG, fg=FG, anchor="w")

        def entry(parent, var, width):
            return tk.Entry(
                parent, textvariable=var, width=width,
                bg=ENTRY_BG, fg=FG,
                insertbackground=FG,
                disabledbackground=ENTRY_BG, disabledforeground=MUTED,
                readonlybackground=ENTRY_BG,
                highlightthickness=1, highlightbackground="#444444",
                highlightcolor=ACCENT, relief="flat",
            )

        def button(parent, text, cmd, **kw):
            return tk.Button(
                parent, text=text, command=cmd,
                bg=BTN_BG, fg=FG,
                activebackground="#555555", activeforeground=FG,
                disabledforeground=MUTED,
                highlightbackground=BG, relief="raised", bd=1,
                **kw,
            )

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", **pad)
        lbl(top, "Excel-файл:").grid(row=0, column=0, sticky="w")
        entry(top, self.file_var, 70).grid(row=0, column=1, sticky="we", padx=4)
        button(top, "Выбрать…", self._choose_file).grid(row=0, column=2, padx=4)
        top.columnconfigure(1, weight=1)

        meta = tk.LabelFrame(
            self, text="Параметры (можно поправить)",
            bg=BG, fg=FG, labelanchor="nw", bd=1, relief="groove",
        )
        meta.pack(fill="x", **pad)
        lbl(meta, "Класс:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        entry(meta, self.klass_var, 10).grid(row=0, column=1, sticky="w", padx=4)
        lbl(meta, "Предмет:").grid(row=0, column=2, sticky="w", padx=4)
        entry(meta, self.subject_var, 40).grid(row=0, column=3, sticky="we", padx=4)
        lbl(meta, "Группа:").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        entry(meta, self.group_var, 20).grid(row=1, column=1, sticky="w", padx=4)
        lbl(meta, "Период:").grid(row=1, column=2, sticky="w", padx=4)
        entry(meta, self.period_var, 20).grid(row=1, column=3, sticky="w", padx=4)
        tk.Checkbutton(
            meta, text="Холостой прогон (ничего не сохранять в БАРС)",
            variable=self.dry_run_var,
            bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
            selectcolor=BG, highlightthickness=0,
        ).grid(row=2, column=0, columnspan=4, sticky="w", padx=4, pady=4)
        meta.columnconfigure(3, weight=1)

        btns = tk.Frame(self, bg=BG)
        btns.pack(fill="x", **pad)
        self.start_btn = button(btns, "Старт", self._start)
        self.start_btn.pack(side="left")
        self.confirm_btn = button(btns, "Я вошёл (продолжить)", self._confirm_login, state="disabled")
        self.confirm_btn.pack(side="left", padx=8)
        self.finish_btn = button(btns, "Завершить (закрыть браузер)", self._finish, state="disabled")
        self.finish_btn.pack(side="left", padx=8)

        self.log = scrolledtext.ScrolledText(
            self, height=24, wrap="word",
            bg=ENTRY_BG, fg=FG,
            insertbackground=FG,
            highlightthickness=1, highlightbackground="#444444",
            relief="flat", borderwidth=0,
        )
        self.log.pack(fill="both", expand=True, **pad)
        self.log.tag_config("ok", foreground="#7ed87e")
        self.log.tag_config("warn", foreground="#f0c060")
        self.log.tag_config("err", foreground="#ff7070")
        self.log.tag_config("info", foreground=MUTED)

    # ───────────── обработчики ─────────────

    def _choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите Excel-файл с расписанием",
            filetypes=[("Excel", "*.xls *.xlsx"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        self.file_var.set(path)
        try:
            schedule = excel_parser.parse(path)
        except Exception as exc:
            messagebox.showerror("Ошибка парсинга", str(exc))
            return
        self.klass_var.set(schedule.klass)
        self.subject_var.set(schedule.subject)
        self.group_var.set(schedule.group)
        self.period_var.set(schedule.period)
        self._log(
            f"Распознано: класс={schedule.klass!r}, предмет={schedule.subject!r}, "
            f"группа={schedule.group!r}, период={schedule.period!r}, "
            f"учитель={schedule.teacher!r}",
            "info",
        )
        self._log(
            f"Уроков всего: {len(schedule.lessons)}; с ДЗ к заливке: {len(schedule.fillable())}.",
            "info",
        )

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        path = self.file_var.get().strip()
        if not path or not Path(path).exists():
            messagebox.showwarning("Нет файла", "Сначала выберите Excel-файл.")
            return
        self.start_btn.config(state="disabled")
        self.confirm_event.clear()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def _confirm_login(self) -> None:
        self.confirm_event.set()
        self.confirm_btn.config(state="disabled")

    def _finish(self) -> None:
        self.finish_event.set()
        self.finish_btn.config(state="disabled")

    def _run(self) -> None:
        try:
            schedule = excel_parser.parse(self.file_var.get())
            schedule.klass = self.klass_var.get().strip() or schedule.klass
            schedule.subject = self.subject_var.get().strip() or schedule.subject
            schedule.group = self.group_var.get().strip() or schedule.group

            # Период: если пользователь ввёл четверть (например «4 четверть»),
            # БАРС покажет только уроки той четверти, а Excel почти всегда содержит
            # всё полугодие → большинство уроков уйдёт в not_found. Автоматически
            # расширяем до полугодия (которое угадал парсер по датам Excel).
            user_period = self.period_var.get().strip()
            guessed = schedule.period  # _guess_period: «1 Полугодие» или «2 Полугодие»
            if not user_period:
                schedule.period = guessed
            elif "четверт" in user_period.lower():
                self._log(
                    f"Период {user_period!r} заменён на {guessed!r} — БАРС с фильтром "
                    "по четверти не покажет все уроки Excel.",
                    "warn",
                )
                schedule.period = guessed
            else:
                schedule.period = user_period
            # Синхронизируем UI с реально используемым периодом.
            self.after(0, lambda: self.period_var.set(schedule.period))

            results = []
            run_error: Exception | None = None
            ctx = None
            with sync_playwright() as p:
                try:
                    ctx = auth.ensure_session(
                        p,
                        confirm_login=self._wait_for_login,
                        log=lambda m: self._log(m, "info"),
                    )
                    client = bars.BarsClient(
                        ctx,
                        dry_run=self.dry_run_var.get(),
                        log=lambda m: self._log(m, "info"),
                    )
                    try:
                        client.open_journal(schedule)
                        results = client.fill_homework(schedule)
                    except Exception as exc:
                        run_error = exc
                        self._log(f"Ошибка во время работы: {exc}", "err")
                    # Браузер открыт, ждём пока пользователь не нажмёт «Завершить».
                    self._log(
                        "Браузер открыт — посмотрите, что в БАРС, и нажмите «Завершить».",
                        "warn",
                    )
                    self.finish_event.clear()
                    self.after(0, lambda: self.finish_btn.config(state="normal"))
                    self.finish_event.wait()
                finally:
                    # Гарантированно закрываем persistent context — иначе процесс
                    # Chromium останется и chrome_profile/ будет lock'нут.
                    if ctx is not None:
                        try:
                            ctx.close()
                        except Exception as exc:
                            self._log(f"Не удалось закрыть browser context: {exc}", "warn")

            if run_error is not None and not results:
                raise run_error

            self._log("─" * 60, "info")
            counts: dict[str, int] = {}
            marked_count = 0
            for r in results:
                counts[r.status] = counts.get(r.status, 0) + 1
                if r.marked_conducted:
                    marked_count += 1
                tag = {
                    "filled": "ok",
                    "skipped_existing": "warn",
                    "skipped_no_homework": "warn",
                    "not_found": "warn",
                    "error": "err",
                }.get(r.status, "info")
                conducted_mark = " ☑" if r.marked_conducted else ""
                self._log(
                    f"[{r.status:>20}]{conducted_mark} {r.date_hint} | {r.topic[:50]}"
                    + (f" → {r.homework!r}" if r.homework else "")
                    + (f"  ({r.note})" if r.note else ""),
                    tag,
                )
            self._log("─" * 60, "info")
            summary = ", ".join(f"{k}={v}" for k, v in counts.items())
            if marked_count:
                summary += f", marked_conducted={marked_count}"
            self._log("Итог: " + summary, "info")
        except Exception as exc:
            self._log(f"Ошибка: {exc}", "err")
        finally:
            self.after(0, lambda: self.start_btn.config(state="normal"))
            self.after(0, lambda: self.confirm_btn.config(state="disabled"))
            self.after(0, lambda: self.finish_btn.config(state="disabled"))

    def _wait_for_login(self) -> None:
        self.after(0, lambda: self.confirm_btn.config(state="normal"))
        self._log("Зайдите в БАРС в открывшемся окне браузера и нажмите «Я вошёл (продолжить)».", "warn")
        # Ждём в коротких циклах, чтобы реагировать на «Завершить» и закрытие окна.
        while not self.confirm_event.wait(0.2):
            if self.finish_event.is_set():
                raise RuntimeError("Пользователь отменил вход (нажата «Завершить»)")

    # ───────────── лог ─────────────

    def _log(self, msg: str, tag: str = "info") -> None:
        self.log_queue.put((msg, tag))
        try:
            with open(self.log_file_path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def _drain_log(self) -> None:
        try:
            while True:
                msg, tag = self.log_queue.get_nowait()
                self.log.insert("end", msg + "\n", tag)
                self.log.see("end")
        except queue.Empty:
            pass
        self.after(100, self._drain_log)


if __name__ == "__main__":
    App().mainloop()
