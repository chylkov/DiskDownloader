#!/usr/bin/env python3
"""Native multi-folder interface; tokens and network work stay out of saved plans."""
from __future__ import annotations

import os
from collections import deque
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import unicodedata
import webbrowser

from yandex_disk.api import Resource, YandexDiskAPI, _normalized_disk_path
from yandex_disk.batch import (BatchRunner, BatchSettings, ConfigurationError,
                               FolderTask, load_settings, save_settings)
from yandex_disk.downloader import RecoveryError, error_text, valid_name
from yandex_disk.credentials import CredentialError, TokenStore
from yandex_disk.ui_clipboard import install_clipboard_support
from yandex_disk.ui_theme import (ACCENT_TEXT, BACKGROUND, CONSOLE, CONSOLE_TEXT, DANGER, LOG_TONES,
                                  SUBTLE, SURFACE, Card, Hero, Tooltip, theme_for)
from yandex_disk.token import normalize_token, TokenError


PROJECT_DIR = Path(__file__).resolve().parent
LEGACY_PATH = PROJECT_DIR / "download.json"
CONFIG_PATH = PROJECT_DIR / "download-gui.json"
LOG_LINES = 2000
OAUTH_TOKEN_URL = (
    "https://oauth.yandex.ru/authorize?response_type=token"
    "&client_id=98979c5296c34f9faea9ab76847efb68"
)
# Bracketed log prefixes and their console colours; any other prefix is a warning.
LOG_PREFIXES = (("[ОШИБКА", "error"), ("[СКАЧАН]", "saved"), ("[ОБНОВЛЁН]", "saved"),
                ("[ПРЕВЬЮ]", "saved"), ("[ПРЕВЬЮ ЕСТЬ]", "quiet"),
                ("[ЕСТЬ]", "quiet"), ("[КРУГ", "cycle"), ("[", "warn"))
LOG_BODIES = {"error": "error_body", "quiet": "quiet"}
TASK_TONES = {"В очереди": "queued", "Копирование…": "running", "Готово": "done", "Есть пропуски": "partial",
              "Ошибка": "failed", "Остановлено": "stopped", "Не начато": "stopped", "Пропустить": "off"}


def _open_token_page(events, generation, revision):
    # Opening a browser can block. Keep the worker independent of Tk objects,
    # including when the user closes the form before the browser responds.
    try:
        opened = bool(webbrowser.open(OAUTH_TOKEN_URL, new=2))
    except Exception:
        opened = False
    events.put(("token_browser_result", {"opened": opened, "generation": generation,
                                        "revision": revision}))


def configure_tk_runtime():
    """Portable CPython distributions can retain a build-time Tcl script path."""
    for variable, folder, marker in (("TCL_LIBRARY", "tcl8.6", "init.tcl"),
                                     ("TK_LIBRARY", "tk8.6", "tk.tcl")):
        current = os.environ.get(variable)
        if current and (Path(current) / marker).is_file():
            continue
        for prefix in (Path(sys.prefix), Path(sys.base_prefix)):
            candidates = (prefix / "tcl" / folder, prefix / "lib" / folder)
            candidate = next((path for path in candidates if (path / marker).is_file()), None)
            if candidate is not None:
                os.environ[variable] = str(candidate)
                break


def display_path(path: str) -> str:
    """Shorten the home folder to ~ for narrow table cells."""
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def size_text(size: int) -> str:
    number = float(size)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if number < 1024 or unit == "ТиБ":
            return f"{number:.1f} {unit}" if unit != "Б" else f"{size} Б"
        number /= 1024
    return str(size)


class DiskDownloaderApp:
    def __init__(self, root: tk.Tk, config_path: Path = CONFIG_PATH,
                 legacy_path: Path | None = LEGACY_PATH, token_store=None):
        self.root = root
        self.config_path = Path(config_path)
        self.events = queue.Queue()
        self.browser_generation = 0
        self.browser_stop = threading.Event()
        self.browser_nodes: dict[str, str] = {}
        self.browser_loading: set[str] = set()
        self.browser_loaded: set[str] = set()
        self.node_counter = 0
        self.runner = None
        self.running = False
        self.closed = False
        self.close_when_done = False
        self.poll_id = None
        self.task_status: dict[int, str] = {}
        self.saved_files = 0
        self.cycle_saved_files = 0
        self.saved_previews = 0
        self.cycle_saved_previews = 0
        self.current_cycle = 0
        self.folder_saved: dict[int, int] = {}
        self.folder_previews: dict[int, int] = {}
        self.controls = []
        self.token_store = token_store if token_store is not None else TokenStore(str(self.config_path.resolve().parent))
        self.credential_jobs = queue.Queue()
        self.credential_generation = 0
        self.credential_pending_count = 0
        self.credential_action = None
        self.token_revision = 0
        self.remembered_token = None
        self.pending_save_token = None
        self.token_browser_pending = False
        load_error = None
        try:
            self.settings = load_settings(self.config_path, legacy_path)
        except (ConfigurationError, OSError, ValueError) as error:
            self.settings = BatchSettings(tasks=[])
            load_error = str(error) if isinstance(error, ConfigurationError) else error_text(error)
        self.token_var = tk.StringVar(value=os.environ.get("YANDEX_DISK_TOKEN", ""))
        self.token_var.trace_add("write", self.token_edited)
        storage_name = getattr(self.token_store, "storage_name", None)
        self.storage_name = storage_name if isinstance(storage_name, str) else "Системное хранилище токенов"
        self.storage_hint = f"Токен запоминается автоматически. Хранилище: {self.storage_name}."
        self.credential_var = tk.StringVar(value=(self.storage_hint if self.token_store.available
                                                  else "Системное хранилище недоступно. Токен можно ввести вручную."))
        self.remote_path_var = tk.StringVar(value="disk:/")
        default_base = (Path(self.settings.tasks[0].output).parent if self.settings.tasks
                        else PROJECT_DIR / "recovered")
        self.base_var = tk.StringVar(value=str(default_base))
        self.status_var = tk.StringVar(value="Добавьте папки и нажмите «Начать копирование»")
        self.connection_var = tk.StringVar(value="Укажите токен для просмотра папок")
        self.workers_var = tk.StringVar(value=str(self.settings.workers))
        self.timeout_var = tk.StringVar(value=f"{self.settings.timeout:g}")
        self.retries_var = tk.StringVar(value=str(self.settings.retries))
        self.verify_var = tk.BooleanVar(value=self.settings.verify)
        self.repeat_var = tk.BooleanVar(value=self.settings.repeat)
        self.repeat_delay_var = tk.StringVar(value=f"{self.settings.repeat_delay:g}")
        self.save_previews_var = tk.BooleanVar(value=self.settings.save_previews)
        self.saved_files_var = tk.StringVar(value="Сохранено за запуск: 0")
        self.previews_var = tk.StringVar(value="Превью: 0")
        self.progress_text_var = tk.StringVar(value="")
        self.configure_style()
        self.build_ui()
        self.clipboard = install_clipboard_support(self.root)
        self.refresh_tasks()
        if load_error:
            self.append_log("Не удалось загрузить план: " + load_error)
            self.status_var.set("Проверьте файл настроек; можно составить новый список")
        elif self.settings.tasks:
            self.append_log("Загружен сохранённый список папок. Для продолжения используйте прежний токен.")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll_id = self.root.after(100, self.poll_events)
        if self.token_store.available:
            threading.Thread(target=self.credential_worker, daemon=True, name="disk-keychain").start()
            if not self.token_var.get().strip():
                self._request_credential("load")

    @property
    def tasks(self):
        return self.settings.tasks

    def configure_style(self):
        self.root.title("DiskDownloader — копирование папок")
        width = min(1180, max(900, self.root.winfo_screenwidth() - 80))
        height = min(880, max(650, self.root.winfo_screenheight() - 110))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(1000, width), min(760, height))
        self.root.configure(background=BACKGROUND)
        self.theme = theme_for(self.root)
        if self.theme.app_icon is not None:
            self.root.iconphoto(True, self.theme.app_icon)

    def button(self, parent, text, command, **kwargs):
        button = ttk.Button(parent, text=text, command=command, **kwargs)
        self.controls.append(button)
        return button

    def section_header(self, parent, number, title):
        header = ttk.Frame(parent)
        ttk.Label(header, text=number, image=self.theme.images["badge"], compound="center",
                  style="Badge.TLabel").pack(side="left")
        ttk.Label(header, text=title, style="Section.TLabel").pack(side="left", padx=(10, 0))
        return header

    def scrolled_tree(self, panel, tree):
        tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(panel, command=tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(panel, orient="horizontal", command=tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)

    def build_ui(self):
        fonts = self.theme.fonts
        self.hero = Hero(self.root, self.theme, title="DiskDownloader",
                         subtitle="Выберите несколько папок. Каждый запуск найдёт новые файлы и продолжит докачку.",
                         status_var=self.connection_var)
        self.hero.pack(fill="x")
        outer = ttk.Frame(self.root, padding=(20, 14, 20, 16), style="App.TFrame")
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=4)
        outer.rowconfigure(2, weight=1)

        connection_card = Card(outer, padding=(18, 12))
        connection_card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        connection = connection_card.body
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="OAuth-токен", style="Section.TLabel").grid(row=0, column=0, padx=(0, 14))
        self.token_entry = ttk.Entry(connection, textvariable=self.token_var, show="•")
        self.token_entry.grid(row=0, column=1, sticky="ew")
        self.controls.append(self.token_entry)
        token_actions = ttk.Frame(connection)
        token_actions.grid(row=0, column=2, padx=(10, 0))
        self.get_token_button = self.button(token_actions, "Получить токен", self.get_token)
        self.get_token_button.pack(side="left", padx=(0, 6))
        self.forget_token_button = self.button(token_actions, "Удалить", self.forget_token, style="Danger.TButton")
        self.forget_token_button.pack(side="left")
        if not self.token_store.available:
            self.forget_token_button.configure(state="disabled")
        self.button(connection, "Подключиться", self.connect, style="Accent.TButton").grid(row=0, column=3, padx=(16, 0))
        ttk.Label(connection, textvariable=self.credential_var, wraplength=700,
                  style="Muted.TLabel").grid(row=1, column=1, columnspan=3, sticky="w", pady=(6, 0))

        panes = ttk.Panedwindow(outer, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew")
        browser_card = Card(panes)
        panes.add(browser_card, weight=2)
        browser = browser_card.body
        browser.columnconfigure(0, weight=1)
        browser.rowconfigure(2, weight=1)
        self.section_header(browser, "1", "Папки на Яндекс Диске").grid(row=0, column=0, sticky="w", pady=(0, 12))
        address = ttk.Frame(browser)
        address.grid(row=1, column=0, sticky="ew")
        address.columnconfigure(0, weight=1)
        self.path_entry = ttk.Entry(address, textvariable=self.remote_path_var)
        self.path_entry.grid(row=0, column=0, sticky="ew")
        self.path_entry.bind("<Return>", lambda _: self.connect())
        self.controls.append(self.path_entry)
        self.button(address, "Открыть", self.connect).grid(row=0, column=1, padx=(8, 0))
        remote_panel = Card(browser, backdrop=SURFACE, radius=10, padding=(4, 4), shadow=False)
        remote_panel.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        self.remote_tree = ttk.Treeview(remote_panel.body, selectmode="extended", show="tree", height=5)
        self.scrolled_tree(remote_panel.body, self.remote_tree)
        self.remote_tree.tag_configure("placeholder", foreground=SUBTLE, font=fonts["small"])
        self.remote_tree.bind("<<TreeviewOpen>>", self.expand_remote)
        self.remote_tree.bind("<Double-1>", self.remote_double_click)
        self.remote_empty = ttk.Label(remote_panel.body, style="Empty.TLabel",
                                      text="Вставьте токен и нажмите «Подключиться»,\nчтобы увидеть папки Диска")
        browser_actions = ttk.Frame(browser)
        browser_actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        add_selected = self.button(browser_actions, "Добавить выбранные →", self.add_selected, style="Accent.TButton")
        add_selected.pack(side="left")
        Tooltip(add_selected, "Несколько папок: Cmd/Ctrl + щелчок; диапазон: Shift.")
        self.button(browser_actions, "Добавить по пути", self.add_manual).pack(side="left", padx=(8, 0))

        plan_card = Card(panes)
        panes.add(plan_card, weight=3)
        plan = plan_card.body
        plan.columnconfigure(0, weight=1)
        plan.rowconfigure(2, weight=1)
        self.section_header(plan, "2", "Очередь копирования").grid(row=0, column=0, sticky="w", pady=(0, 12))
        destination = ttk.Frame(plan)
        destination.grid(row=1, column=0, sticky="ew")
        destination.columnconfigure(1, weight=1)
        caption = ttk.Label(destination, text="Сохранять в", style="Field.TLabel")
        caption.grid(row=0, column=0, padx=(0, 10))
        Tooltip(caption, "Для новых строк внутри этой папки создаются отдельные каталоги.")
        self.base_entry = ttk.Entry(destination, textvariable=self.base_var)
        self.base_entry.grid(row=0, column=1, sticky="ew")
        self.controls.append(self.base_entry)
        self.button(destination, "Выбрать…", self.choose_base).grid(row=0, column=2, padx=(8, 0))
        tasks_panel = Card(plan, backdrop=SURFACE, radius=10, padding=(4, 4), shadow=False)
        tasks_panel.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        self.task_tree = ttk.Treeview(tasks_panel.body, columns=("enabled", "source", "output", "status"),
                                      show="tree headings", selectmode="extended", height=5, style="Tasks.Treeview")
        self.task_tree.column("#0", width=30, minwidth=30, stretch=False)
        # Tk shares extra width equally between stretching columns, so the
        # short ones keep a fixed width and the two paths take the rest.
        for key, title, width in (("enabled", "Вкл.", 48), ("source", "На Диске", 130),
                                  ("output", "На компьютере", 180), ("status", "Состояние", 130)):
            self.task_tree.heading(key, text=title, anchor="center" if key == "enabled" else "w")
            self.task_tree.column(key, width=width, minwidth=width if key == "enabled" else 90,
                                  stretch=key in ("source", "output"),
                                  anchor="center" if key == "enabled" else "w")
        self.scrolled_tree(tasks_panel.body, self.task_tree)
        self.task_tree.tag_configure("off", foreground=SUBTLE)
        self.task_tree.tag_configure("running", foreground=ACCENT_TEXT)
        self.task_tree.tag_configure("failed", foreground=DANGER)
        self.task_tree.bind("<Button-1>", self.task_click)
        self.tasks_empty = ttk.Label(tasks_panel.body, style="Empty.TLabel",
                                     text="Очередь пуста\nВыделите папки слева и нажмите «Добавить выбранные»")
        task_actions = ttk.Frame(plan)
        task_actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.button(task_actions, "Куда сохранить…", self.change_destination).pack(side="left")
        self.button(task_actions, "Вкл. / выкл.", self.toggle_selected).pack(side="left", padx=8)
        remove = self.button(task_actions, "Удалить из списка", self.remove_selected, style="Danger.TButton")
        remove.pack(side="left")
        Tooltip(remove, "Строка удаляется из очереди, скачанные файлы остаются на месте. "
                        "Список сохраняется автоматически.")

        bottom = ttk.Frame(outer, style="App.TFrame")
        bottom.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        bottom.columnconfigure(1, weight=1)
        bottom.rowconfigure(0, weight=1)
        run_card = Card(bottom)
        run_card.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        # Keep all run controls visible when the window is at its minimum size;
        # the browser and queue can shrink around their scrollable trees.
        run_card.bind("<Configure>",
                      lambda _event: outer.rowconfigure(2, minsize=run_card.winfo_reqheight() + 12), add="+")
        run = run_card.body
        self.section_header(run, "3", "Параметры и запуск").grid(row=0, column=0, sticky="w", pady=(0, 12))
        numbers = ttk.Frame(run)
        numbers.grid(row=1, column=0, sticky="w")
        for label, variable, low, high, width, hint in (
                ("Потоков", self.workers_var, 1, 32, 3, "Сколько файлов одной папки скачивается одновременно: 1–32."),
                ("Таймаут, с", self.timeout_var, 1, 600, 4, "Сколько ждать ответа сети: 1–600 секунд."),
                ("Повторов", self.retries_var, 0, 10, 3, "Повторы после сетевой ошибки сверх первой попытки: 0–10.")):
            caption = ttk.Label(numbers, text=label, style="Field.TLabel")
            caption.pack(side="left", padx=(0, 8))
            Tooltip(caption, hint)
            control = ttk.Spinbox(numbers, from_=low, to=high, width=width, textvariable=variable)
            control.pack(side="left", padx=(0, 18))
            self.controls.append(control)
        switches = ttk.Frame(run)
        switches.grid(row=2, column=0, sticky="w", pady=(10, 0))
        repeat = ttk.Checkbutton(switches, text="Проходить по кругу", variable=self.repeat_var)
        repeat.pack(side="left")
        self.controls.append(repeat)
        ttk.Label(switches, text="пауза, с", style="Field.TLabel").pack(side="left", padx=(10, 8))
        repeat_delay = ttk.Spinbox(switches, from_=1, to=86400, increment=1,
                                   width=5, textvariable=self.repeat_delay_var)
        repeat_delay.pack(side="left")
        Tooltip(repeat_delay, "Пауза между кругами: 1–86 400 секунд.")
        self.controls.append(repeat_delay)
        verify = ttk.Checkbutton(switches, text="Сверять SHA-256", variable=self.verify_var)
        verify.pack(side="left", padx=(22, 0))
        Tooltip(verify, "Пересчитывать SHA-256 уже сохранённых файлов. Дольше, зато находит "
                        "локальные повреждения при неизменных размере и времени.")
        self.controls.append(verify)
        previews = ttk.Checkbutton(run, text="Сохранять превью при ошибке оригинала",
                                   variable=self.save_previews_var)
        previews.grid(row=3, column=0, sticky="w", pady=(8, 0))
        Tooltip(previews, "Если оригинал недоступен, сохранить уменьшенное изображение на его месте "
                          "под тем же именем. Эта копия учитывается как превью; в следующем круге "
                          "или запуске приложение снова попробует скачать оригинал, даже без сверки SHA-256. "
                          "Уже сохранённый оригинал превью не заменяет.")
        self.controls.append(previews)
        actions = ttk.Frame(run)
        actions.grid(row=4, column=0, sticky="w", pady=(16, 0))
        self.start_button = ttk.Button(actions, text="Начать копирование", command=self.start_download,
                                       style="Large.Accent.TButton")
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="Остановить", command=self.stop_download, state="disabled",
                                      style="Large.Danger.TButton")
        self.stop_button.pack(side="left", padx=(10, 0))

        log_card = Card(bottom, fill=CONSOLE, outline=CONSOLE, body_style="Console.TFrame")
        log_card.grid(row=0, column=1, sticky="nsew")
        log_frame = log_card.body
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(3, weight=1)
        log_header = ttk.Frame(log_frame, style="Console.TFrame")
        log_header.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(log_header, text="Ход копирования", style="ConsoleTitle.TLabel").pack(side="left")
        ttk.Label(log_header, textvariable=self.saved_files_var, style="ConsoleChip.TLabel").pack(side="right")
        status = ttk.Label(log_frame, textvariable=self.status_var, style="Console.TLabel", wraplength=480)
        status.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 8))
        log_frame.bind("<Configure>", lambda event: status.configure(wraplength=max(240, event.width - 8)))
        progress_row = ttk.Frame(log_frame, style="Console.TFrame")
        progress_row.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.progress = ttk.Progressbar(progress_row, mode="determinate", maximum=1)
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(progress_row, textvariable=self.progress_text_var,
                  style="ConsoleMuted.TLabel").pack(side="left", padx=(12, 0))
        previews_count = ttk.Label(progress_row, textvariable=self.previews_var, style="ConsoleMuted.TLabel")
        previews_count.pack(side="left", padx=(12, 0))
        Tooltip(previews_count, "Уменьшенные изображения, сохранённые за этот запуск во всех кругах. "
                                "Они учитываются отдельно от оригиналов.")
        self.log_widget = tk.Text(log_frame, height=4, state="disabled", wrap="word", background=CONSOLE,
                                  foreground=CONSOLE_TEXT, insertbackground="white", selectbackground="#2B3A57",
                                  relief="flat", borderwidth=0, highlightthickness=0, padx=0, pady=2,
                                  spacing1=1, spacing3=1, font=fonts["mono"])
        self.log_widget.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
        for tone, color in LOG_TONES.items():
            self.log_widget.tag_configure(tone, foreground=color)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_widget.yview, style="Console.Vertical.TScrollbar")
        log_scroll.grid(row=3, column=1, sticky="ns", pady=(12, 0))
        self.log_widget.configure(yscrollcommand=log_scroll.set)

    def refresh_empty_states(self):
        for label, tree in ((self.remote_empty, self.remote_tree), (self.tasks_empty, self.task_tree)):
            if tree.get_children():
                label.place_forget()
            else:
                label.place(relx=0.5, rely=0.5, anchor="center")

    def set_connection(self, text, tone):
        self.hero.set_tone(tone)
        self.connection_var.set(text)

    def update_progress(self, value, maximum=None):
        if maximum is not None:
            self.progress.configure(maximum=maximum)
        self.progress.configure(value=value)
        self.progress_text_var.set(f"Папки: {int(float(self.progress['value']))} из "
                                   f"{int(float(self.progress['maximum']))}")

    def show_error(self, text):
        messagebox.showerror("DiskDownloader", text, parent=self.root)

    def token_edited(self, *_args):
        self.token_revision += 1

    def get_token(self):
        if self.running or self.closed or self.token_browser_pending:
            return
        self.token_browser_pending = True
        self.get_token_button.configure(state="disabled")
        self.credential_var.set("Открываем страницу получения токена в браузере…")
        generation, revision = self.credential_generation, self.token_revision
        try:
            threading.Thread(target=_open_token_page, args=(self.events, generation, revision),
                             daemon=True, name="disk-oauth-browser").start()
        except RuntimeError:
            self.handle_event("token_browser_result", {"opened": False, "generation": generation,
                                                        "revision": revision})

    def paste_token(self):
        if not self.running:
            self.clipboard.select_all(self.token_entry)
            if self.clipboard.paste(self.token_entry):
                try:
                    self._validated_token()
                except TokenError as error:
                    self.credential_var.set(str(error))
            else:
                self.credential_var.set("В буфере обмена нет текста для вставки.")

    def _validated_token(self):
        token = normalize_token(self.token_var.get())
        if token != self.token_var.get():
            self.token_var.set(token)
        return token

    def token_error(self, error):
        self.credential_var.set(str(error))
        self.show_error(str(error))

    def _request_credential(self, action, token=None):
        if not self.token_store.available or self.closed:
            return
        self.credential_generation += 1
        self.credential_pending_count += 1
        self.credential_action = action
        labels = {"load": "Читаем сохранённый токен…",
                  "save": "Сохраняем токен в системном хранилище…",
                  "delete": "Удаляем сохранённый токен…"}
        self.credential_var.set(labels[action])
        self.credential_jobs.put((action, self.credential_generation, token, self.token_revision))

    def credential_worker(self):
        while True:
            job = self.credential_jobs.get()
            try:
                if job is None:
                    return
                action, generation, token, revision = job
                error = None
                try:
                    if action == "load":
                        token = self.token_store.load()
                    elif action == "save":
                        self.token_store.save(token)
                    else:
                        self.token_store.delete()
                except CredentialError as exception:
                    error = str(exception)
                except Exception:
                    error = "Не удалось обратиться к хранилищу токенов. Токен можно ввести вручную."
                self.events.put(("credential_result", {"action": action, "generation": generation,
                                                        "revision": revision, "token": token, "error": error}))
            finally:
                self.credential_jobs.task_done()

    def _remember_token(self, token):
        if (self.token_store.available and token != self.pending_save_token
                and (self.pending_save_token is not None or token != self.remembered_token)):
            self.pending_save_token = token
            self._request_credential("save", token)

    def save_token(self):
        if self.running or not self.token_store.available:
            return
        try:
            token = self._validated_token()
        except TokenError as error:
            self.token_error(error)
            return
        self._remember_token(token)

    def forget_token(self):
        if self.running or not self.token_store.available:
            return
        self.token_var.set("")
        self.remembered_token = None
        self.pending_save_token = None
        self._request_credential("delete")

    def append_log(self, *messages):
        # Bound retained UI text; durable per-folder logs remain in the backup.
        if not messages:
            return
        stamp = time.strftime("%H:%M:%S  ")
        parts = []
        for message in messages:
            tone = next((tone for prefix, tone in LOG_PREFIXES if message.startswith(prefix)), None)
            head, separator, body = message.partition("] ")
            parts += [stamp, "time"]
            if tone and separator:
                parts += [head + "]", tone, " " + body + "\n", LOG_BODIES.get(tone, "plain")]
            else:
                parts += [message + "\n", tone or "plain"]
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", *parts)
        lines = int(self.log_widget.index("end-1c").split(".")[0])
        if lines > LOG_LINES:
            self.log_widget.delete("1.0", f"{lines - LOG_LINES * 9 // 10}.0")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def poll_events(self):
        if self.closed:
            return
        # A bounded slice of work per tick keeps clicks responsive while a run
        # floods the queue. Consecutive log lines are inserted in one call, and
        # only as many as the widget retains: older ones would be trimmed anyway.
        deadline = time.monotonic() + 0.03
        lines = deque(maxlen=LOG_LINES)
        while time.monotonic() < deadline:
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                lines.append(data["message"])
                continue
            self.append_log(*lines)
            lines.clear()
            self.handle_event(kind, data)
            if self.closed:
                return
        self.append_log(*lines)
        self.poll_id = self.root.after(50 if not self.events.empty() else 100, self.poll_events)

    def connect(self):
        if self.running:
            return
        try:
            token = self._validated_token()
        except TokenError as error:
            self.token_error(error)
            return
        try:
            path = _normalized_disk_path(self.remote_path_var.get())
        except ValueError as error:
            self.show_error(str(error))
            return
        self._remember_token(token)
        self.browser_generation += 1
        self.browser_stop.set()
        self.browser_stop = threading.Event()
        self.browser_nodes.clear()
        self.browser_loading.clear()
        self.browser_loaded.clear()
        self.remote_tree.delete(*self.remote_tree.get_children())
        self.remote_empty.configure(text="Загружаем папки…")
        self.refresh_empty_states()
        self.set_connection("Загрузка списка папок…", "busy")
        self.load_remote(path)

    def load_remote(self, path, parent_iid=None):
        generation = self.browser_generation
        stop = self.browser_stop
        try:
            token = self._validated_token()
        except TokenError as error:
            self.token_error(error)
            return
        if parent_iid is not None:
            self.browser_loading.add(parent_iid)

        def worker():
            folders = []
            error = None
            try:
                api = YandexDiskAPI(disk_path=path, token=token, timeout=30, retries=1,
                                    log=lambda text: self.events.put(("browser_log", {"generation": generation, "message": text})))
                api.stop_event = stop
                resource = api.get_resource(path)
                if resource.kind != "dir":
                    raise ConfigurationError("Указанный путь ведёт к файлу. Выберите папку.")
                for child in api.iter_children(path):
                    if child.kind == "dir":
                        expected = path.rstrip("/") + "/" + child.name
                        try:
                            valid_name(child.name)
                            if child.path != expected:
                                raise RecoveryError("API вернул папку с неожиданным путём")
                        except RecoveryError:
                            error = "Часть папок пропущена из-за некорректных имён или путей API"
                            continue
                        folders.append(child)
            except Exception as exception:
                error = str(exception) if isinstance(exception, ConfigurationError) else error_text(exception)
            self.events.put(("remote_result", {"generation": generation, "parent": parent_iid,
                                                "path": path, "folders": folders, "error": error}))

        threading.Thread(target=worker, daemon=True, name="disk-folder-browser").start()

    def insert_remote(self, parent, name, path, icon="folder"):
        self.node_counter += 1
        iid = "remote-" + str(self.node_counter)
        self.remote_tree.insert(parent, "end", iid=iid, text=name, image=self.theme.images[icon])
        self.remote_tree.insert(iid, "end", text="Открыть подпапки…", tags=("placeholder",))
        self.browser_nodes[iid] = path
        return iid

    def expand_remote(self, _event=None):
        if self.running:
            return
        iid = self.remote_tree.focus()
        if iid in self.browser_nodes and iid not in self.browser_loading and iid not in self.browser_loaded:
            self.load_remote(self.browser_nodes[iid], iid)

    def remote_double_click(self, event):
        if self.running:
            return
        iid = self.remote_tree.identify_row(event.y)
        if iid and "placeholder" in self.remote_tree.item(iid, "tags"):
            parent = self.remote_tree.parent(iid)
            if parent in self.browser_nodes and parent not in self.browser_loading and parent not in self.browser_loaded:
                self.load_remote(self.browser_nodes[parent], parent)
            return "break"

    def refresh_saved_counter(self):
        text = f"Сохранено за запуск: {self.saved_files}"
        if self.settings.repeat and self.current_cycle:
            text += f" · в круге {self.current_cycle}: {self.cycle_saved_files}"
        self.saved_files_var.set(text)
        self.previews_var.set(f"Превью: {self.saved_previews}")

    def count_saved(self, index, cycle, downloaded, updated, previews=0):
        if self.current_cycle == 0:
            self.current_cycle = cycle
        if cycle != self.current_cycle:
            return
        saved = max(0, int(downloaded)) + max(0, int(updated))
        previous = self.folder_saved.get(index, 0)
        if saved > previous:
            self.folder_saved[index] = saved
            difference = saved - previous
            self.saved_files += difference
            self.cycle_saved_files += difference
        previews = max(0, int(previews))
        previous_previews = self.folder_previews.get(index, 0)
        if previews > previous_previews:
            self.folder_previews[index] = previews
            difference = previews - previous_previews
            self.saved_previews += difference
            self.cycle_saved_previews += difference
        self.refresh_saved_counter()

    def count_result(self, result, cycle):
        for item in result.items:
            if item.report is not None:
                self.count_saved(item.index, cycle, item.report.downloaded, item.report.updated,
                                 item.report.previews)

    def handle_event(self, kind, data):
        if self.closed:
            return
        if kind == "token_browser_result":
            self.token_browser_pending = False
            if not self.running:
                self.get_token_button.configure(state="normal")
            if (not self.running and data.get("generation") == self.credential_generation
                    and data.get("revision") == self.token_revision):
                if data["opened"]:
                    self.credential_var.set("На странице Яндекса нажмите «Разрешить», скопируйте токен "
                                            "и вставьте его сюда через Cmd/Ctrl+V. Затем нажмите «Подключиться».")
                else:
                    self.credential_var.set("Не удалось открыть браузер. Откройте ссылку получения токена вручную.")
                    self.show_error("Не удалось открыть браузер. Откройте эту ссылку вручную:\n" + OAUTH_TOKEN_URL)
            return
        if kind == "credential_result":
            self.credential_pending_count = max(0, self.credential_pending_count - 1)
            if data["action"] == "save" and self.pending_save_token == data.get("token"):
                self.pending_save_token = None
            if data.get("generation") == self.credential_generation:
                if data.get("error"):
                    self.credential_var.set(data["error"])
                    self.append_log("[ХРАНИЛИЩЕ ТОКЕНОВ] " + data["error"])
                elif data["action"] == "load":
                    if (data.get("revision") == self.token_revision and not self.token_var.get().strip()
                            and data.get("token")):
                        self.token_var.set(data["token"])
                        self.remembered_token = data["token"]
                        self.credential_var.set("Сохранённый токен подставлен из системного хранилища.")
                    else:
                        self.credential_var.set(self.storage_hint)
                elif data["action"] == "save":
                    self.remembered_token = data["token"]
                    self.credential_var.set(f"Токен сохранён. Хранилище: {self.storage_name}.")
                else:
                    self.remembered_token = None
                    self.credential_var.set("Сохранённый токен удалён из системного хранилища.")
            if self.close_when_done and not self.running and not self.credential_pending_count:
                self.destroy()
            return
        if kind in {"remote_result", "browser_log"}:
            if data.get("generation") != self.browser_generation:
                return
        if kind == "browser_log":
            self.append_log(data["message"])
        elif kind == "remote_result":
            parent = data["parent"]
            if parent is None:
                label = "Весь Диск" if data["path"] == "disk:/" else data["path"][5:]
                parent = self.insert_remote("", label, data["path"], icon="cloud")
                self.remote_tree.item(parent, open=True)
                self.remote_path_var.set(data["path"])
                self.refresh_empty_states()
            elif not self.remote_tree.exists(parent):
                return
            self.browser_loading.discard(parent)
            self.remote_tree.delete(*self.remote_tree.get_children(parent))
            for folder in data["folders"]:
                self.insert_remote(parent, folder.name, folder.path)
            if data["error"]:
                self.set_connection("Часть списка недоступна — можно повторить открытие", "warn")
                self.append_log("[ОБЗОР ПАПОК] " + data["error"])
                self.remote_tree.insert(parent, "end", text="Повторить открытие папки", tags=("placeholder",))
            else:
                self.browser_loaded.add(parent)
                self.set_connection("Подключено · выберите папки слева", "ok")
                if not data["folders"]:
                    self.remote_tree.insert(parent, "end", text="Подпапок нет", tags=("placeholder",))
        elif kind == "log":
            self.append_log(data["message"])
        elif kind == "progress":
            self.count_saved(data["index"], data.get("cycle", self.current_cycle or 1),
                             data.get("downloaded", 0), data.get("updated", 0), data.get("previews", 0))
        elif kind == "cycle_started":
            cycle = data["cycle"]
            if cycle < self.current_cycle:
                return
            if cycle != self.current_cycle:
                self.folder_saved.clear()
                self.cycle_saved_files = 0
                self.folder_previews.clear()
                self.cycle_saved_previews = 0
            self.current_cycle = cycle
            self.task_status.clear()
            self.refresh_tasks()
            self.update_progress(0, sum(task.enabled for task in self.tasks))
            self.refresh_saved_counter()
            self.status_var.set(f"Начинается круг {cycle}…")
            self.append_log(f"[КРУГ {cycle}] Обход всех выбранных папок")
        elif kind == "cycle_finished":
            cycle = data["cycle"]
            result = data["result"]
            self.count_result(result, cycle)
            title = "остановлен" if result.cancelled else "завершён"
            self.append_log(f"[КРУГ {cycle}] {title}; сохранено: {result.downloaded + result.updated}; "
                            f"превью: {result.previews}; уже есть: {result.skipped}; ошибок: {result.errors}.")
        elif kind == "cycle_wait":
            self.status_var.set(f"Круг {data['cycle']} завершён. Следующий обход через {data['delay']:g} с.")
            self.append_log(f"[ОЖИДАНИЕ] Следующий круг через {data['delay']:g} с. Кнопка «Остановить» доступна.")
        elif kind == "task_started":
            self.task_status[data["index"]] = "Копирование…"
            self.refresh_tasks()
            self.status_var.set("Копируется " + data["task"].disk_path[5:])
        elif kind == "task_finished":
            result = data["result"]
            if result.report is not None:
                self.count_saved(data["index"], data.get("cycle", self.current_cycle or 1),
                                 result.report.downloaded, result.report.updated, result.report.previews)
            labels = {"complete": "Готово", "partial": "Есть пропуски", "failed": "Ошибка",
                      "cancelled": "Остановлено", "not_started": "Не начато"}
            self.task_status[data["index"]] = labels.get(result.status, result.status)
            self.refresh_tasks()
            self.update_progress(min(float(self.progress["value"]) + 1, float(self.progress["maximum"])))
            if result.error:
                self.append_log("[ОШИБКА ПАПКИ] " + result.error)
        elif kind == "finished":
            result = data["result"]
            final_cycle = max(self.current_cycle, result.cycles)
            if final_cycle != self.current_cycle:
                self.folder_saved.clear()
                self.cycle_saved_files = 0
                self.folder_previews.clear()
                self.cycle_saved_previews = 0
                self.current_cycle = final_cycle
            self.count_result(result, final_cycle)
            self.saved_files = max(self.saved_files, result.downloaded + result.updated)
            self.saved_previews = max(self.saved_previews, result.previews)
            self.refresh_saved_counter()
            for item in result.items:
                if item.status == "not_started":
                    self.task_status[item.index] = "Не начато"
            self.refresh_tasks()
            title = "Остановлено" if result.cancelled else "Копирование завершено"
            summary = (f"{title}. Скачано: {result.downloaded}; обновлено: {result.updated}; "
                       f"превью: {result.previews}; уже есть: {result.skipped}; ошибок: {result.errors}.")
            self.status_var.set(summary)
            self.append_log(summary + f" Получено: {size_text(result.bytes_received)}.")
            if result.errors:
                self.append_log("При следующем запуске недоступные файлы будут проверены снова.")
            self.set_running(False)
            if self.close_when_done and not self.credential_pending_count:
                self.destroy()
        elif kind == "batch_error":
            self.append_log("[ОШИБКА] " + data["message"])
            self.status_var.set("Запуск завершён с ошибкой. Скачанные файлы сохранены.")
            self.set_running(False)
            if self.close_when_done and not self.credential_pending_count:
                self.destroy()

    def collect_settings(self, tasks=None):
        try:
            return BatchSettings(tasks=list(self.tasks if tasks is None else tasks),
                                 workers=int(self.workers_var.get()), retries=int(self.retries_var.get()),
                                 timeout=float(self.timeout_var.get()), verify=self.verify_var.get(),
                                 repeat=self.repeat_var.get(), repeat_delay=float(self.repeat_delay_var.get()),
                                 save_previews=self.save_previews_var.get())
        except (ValueError, TypeError) as error:
            if isinstance(error, ConfigurationError):
                raise
            raise ConfigurationError("Проверьте числа: потоки 1–32, повторы 0–10, таймаут 1–600 с, пауза между кругами 1–86400 с.") from None

    def commit_tasks(self, tasks):
        settings = self.collect_settings(tasks)
        save_settings(self.config_path, settings)
        self.settings = settings
        self.task_status.clear()
        self.refresh_tasks()

    def add_tasks(self, paths):
        if self.running:
            return
        try:
            if not self.base_var.get().strip():
                raise ConfigurationError("Выберите локальную папку для новых строк.")
            base = Path(self.base_var.get()).expanduser().resolve()
            tasks = list(self.tasks)
            sources = {task.disk_path for task in tasks}
            occupied = {unicodedata.normalize("NFC", task.output).casefold() for task in tasks}
            for path in paths:
                source = _normalized_disk_path(path)
                if source in sources:
                    continue
                leaf = source.rstrip("/").rsplit("/", 1)[-1] if source != "disk:/" else "Весь Диск"
                valid_name(leaf)
                output = base / leaf
                suffix = 2
                while unicodedata.normalize("NFC", str(output)).casefold() in occupied:
                    output = base / f"{leaf} ({suffix})"
                    suffix += 1
                tasks.append(FolderTask(source, str(output)))
                sources.add(source)
                occupied.add(unicodedata.normalize("NFC", str(output)).casefold())
            added = len(tasks) - len(self.tasks)
            self.commit_tasks(tasks)
            self.status_var.set(f"Добавлено папок: {added}. Проверьте назначения и начните копирование.")
        except RuntimeError:
            self.show_error("Не удалось определить локальную папку. Проверьте путь и символические ссылки.")
        except (ConfigurationError, ValueError, OSError, RecoveryError) as error:
            self.show_error(str(error) if isinstance(error, ValueError) else error_text(error))

    def add_selected(self):
        paths = [self.browser_nodes[iid] for iid in self.remote_tree.selection() if iid in self.browser_nodes]
        if not paths:
            self.show_error("Выберите одну или несколько папок в списке слева.")
            return
        self.add_tasks(paths)

    def add_manual(self):
        path = simpledialog.askstring("Папка на Диске", "Введите путь, например /Фото/2019:", parent=self.root)
        if path:
            self.add_tasks([path])

    def choose_base(self):
        folder = filedialog.askdirectory(title="Куда сохранять новые папки",
                                         initialdir=self.initial_directory(self.base_var.get()), parent=self.root)
        if folder:
            self.base_var.set(folder)

    def initial_directory(self, value):
        try:
            path = Path(value).expanduser().resolve()
            while not path.is_dir() and path != path.parent:
                path = path.parent
            return str(path) if path.is_dir() else str(PROJECT_DIR)
        except (OSError, RuntimeError, ValueError):
            return str(PROJECT_DIR)

    def selected_indices(self):
        return [int(iid.removeprefix("task-")) for iid in self.task_tree.selection()]

    def change_destination(self):
        if self.running:
            return
        selection = self.selected_indices()
        if len(selection) != 1:
            self.show_error("Выберите одну строку, чтобы изменить её папку сохранения.")
            return
        index = selection[0]
        task = self.tasks[index]
        folder = filedialog.askdirectory(title="Папка назначения для " + task.disk_path[5:],
                                         initialdir=self.initial_directory(task.output),
                                         parent=self.root)
        if folder:
            tasks = list(self.tasks)
            try:
                tasks[index] = FolderTask(task.disk_path, folder, task.enabled)
                self.commit_tasks(tasks)
            except (ConfigurationError, OSError) as error:
                self.show_error(str(error) if isinstance(error, ConfigurationError) else error_text(error))

    def toggle_selected(self):
        if self.running:
            return
        tasks = list(self.tasks)
        for index in self.selected_indices():
            task = tasks[index]
            tasks[index] = FolderTask(task.disk_path, task.output, not task.enabled)
        try:
            self.commit_tasks(tasks)
        except (ConfigurationError, OSError) as error:
            self.show_error(str(error) if isinstance(error, ConfigurationError) else error_text(error))

    def task_click(self, event):
        if not self.running and self.task_tree.identify_column(event.x) == "#1":
            row = self.task_tree.identify_row(event.y)
            if row:
                self.task_tree.selection_set(row)
                self.toggle_selected()
                return "break"

    def remove_selected(self):
        if self.running:
            return
        indices = set(self.selected_indices())
        try:
            self.commit_tasks([task for index, task in enumerate(self.tasks) if index not in indices])
            self.task_status.clear()
            self.refresh_tasks()
        except (ConfigurationError, OSError) as error:
            self.show_error(str(error) if isinstance(error, ConfigurationError) else error_text(error))

    def refresh_tasks(self):
        selected = set(self.task_tree.selection())
        self.task_tree.delete(*self.task_tree.get_children())
        for index, task in enumerate(self.tasks):
            iid = "task-" + str(index)
            status = self.task_status.get(index, "В очереди") if task.enabled else "Пропустить"
            tone = TASK_TONES.get(status, "queued")
            self.task_tree.insert("", "end", iid=iid, image=self.theme.images["dot-" + tone], tags=(tone,),
                                  values=("✓" if task.enabled else "", task.disk_path[5:],
                                          display_path(task.output), status))
            if iid in selected:
                self.task_tree.selection_add(iid)
        self.refresh_empty_states()

    def set_running(self, running):
        self.running = running
        for control in self.controls:
            control.configure(state="disabled" if running else "normal")
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        if self.token_browser_pending:
            self.get_token_button.configure(state="disabled")
        if not self.token_store.available:
            self.forget_token_button.configure(state="disabled")

    def start_download(self):
        if self.running:
            return
        try:
            settings = self.collect_settings()
            if not any(task.enabled for task in settings.tasks):
                raise ConfigurationError("Добавьте и включите хотя бы одну папку для копирования.")
            token = self._validated_token()
            save_settings(self.config_path, settings)
            runner = BatchRunner(settings, token, emit=lambda kind, data: self.events.put((kind, data)))
        except TokenError as error:
            self.token_error(error)
            return
        except (ConfigurationError, ValueError, OSError) as error:
            self.show_error(str(error) if isinstance(error, ValueError) else error_text(error))
            return
        self.settings = settings
        self._remember_token(token)
        self.runner = runner
        self.saved_files = 0
        self.cycle_saved_files = 0
        self.saved_previews = 0
        self.cycle_saved_previews = 0
        self.current_cycle = 1
        self.folder_saved.clear()
        self.folder_previews.clear()
        self.refresh_saved_counter()
        self.task_status.clear()
        self.refresh_tasks()
        self.update_progress(0, sum(task.enabled for task in settings.tasks))
        # Ignore late browsing results and keep all Tk operations on the main thread.
        self.browser_generation += 1
        self.browser_stop.set()
        self.browser_stop = threading.Event()
        self.browser_loading.clear()
        self.set_running(True)
        self.status_var.set("Начинается копирование…")

        def worker():
            try:
                runner.run()
            except Exception as error:
                self.events.put(("batch_error", {"message": error_text(error)}))

        threading.Thread(target=worker, daemon=True, name="disk-batch-download").start()

    def stop_download(self):
        if self.running and self.runner is not None:
            self.runner.stop()
            self.stop_button.configure(state="disabled")
            self.status_var.set("Остановка… сохраняем прогресс и завершаем текущие операции")

    def on_close(self):
        if self.running:
            self.close_when_done = True
            self.stop_download()
        elif self.credential_pending_count and self.credential_action in {"save", "delete"}:
            self.close_when_done = True
            self.status_var.set("Завершаем работу с хранилищем токенов…")
        else:
            self.destroy()

    def destroy(self):
        self.closed = True
        self.credential_jobs.put(None)
        self.browser_stop.set()
        self.token_var.set("")
        if self.poll_id is not None:
            try:
                self.root.after_cancel(self.poll_id)
            except tk.TclError:
                pass
        self.root.destroy()


def main():
    configure_tk_runtime()
    try:
        root = tk.Tk()
    except tk.TclError:
        print("Не удалось открыть оконный интерфейс. Нужен Python 3.10+ с Tkinter и доступ к рабочему столу.", file=sys.stderr)
        return 1
    DiskDownloaderApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
