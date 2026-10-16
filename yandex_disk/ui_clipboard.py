"""Consistent clipboard shortcuts for Tk forms, including macOS Command keys.

An extra bindtag handles editing before Tk's class bindings. The existing class
bindings and virtual-event mappings are kept intact, and each handled shortcut
returns ``break`` so a paste is never also applied by the default binding.
"""
from __future__ import annotations

import tkinter as tk


EDITABLE_CLASSES = frozenset({"Entry", "TEntry", "Spinbox", "TSpinbox", "Text"})
RUSSIAN_KEYS = {"v": ("Cyrillic_em", "Cyrillic_EM"),
                "c": ("Cyrillic_es", "Cyrillic_ES"),
                "x": ("Cyrillic_che", "Cyrillic_CHE"),
                "a": ("Cyrillic_ef", "Cyrillic_EF")}


class ClipboardSupport:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.bindtag = f"DiskDownloaderClipboard{id(self)}"
        self.context = None
        self.edit_menu = None
        self.window_system = str(root.tk.call("tk", "windowingsystem"))
        operations = {"v": self.paste, "c": self.copy, "x": self.cut, "a": self.select_all}
        for key, operation in operations.items():
            for modifier in (("Command", "Control") if self.window_system == "aqua" else ("Control",)):
                # macOS/Tk may report the current Russian layout's keysym,
                # although the user presses the physical V/C/X/A key.
                for letter in (key, key.upper(), *RUSSIAN_KEYS[key]):
                    root.bind_class(self.bindtag, f"<{modifier}-{letter}>", self._handler(operation))
        for sequence, operation in (("<<Paste>>", self.paste), ("<<Copy>>", self.copy),
                                    ("<<Cut>>", self.cut), ("<<SelectAll>>", self.select_all)):
            root.bind_class(self.bindtag, sequence, self._handler(operation))
        root.bind_class(self.bindtag, "<Button-3>", self.show_context_menu)
        if self.window_system == "aqua":
            root.bind_class(self.bindtag, "<Button-2>", self.show_context_menu)
            root.bind_class(self.bindtag, "<Control-Button-1>", self.show_context_menu)
        self._attach_tree(root)
        # This also includes Entry widgets created later in Tk dialogs.
        root.bind_all("<Map>", lambda event: self.attach(event.widget), add="+")
        if self.window_system == "aqua":
            self._install_edit_menu()

    def _handler(self, operation):
        def handle(event):
            operation(event.widget)
            return "break"
        return handle

    def attach(self, widget):
        try:
            if widget.winfo_class() not in EDITABLE_CLASSES:
                return
            tags = list(widget.bindtags())
            if self.bindtag not in tags:
                # Per-widget bindings retain priority; class editing follows us.
                tags.insert(1, self.bindtag)
                widget.bindtags(tuple(tags))
        except (AttributeError, tk.TclError):
            pass

    def _attach_tree(self, widget):
        self.attach(widget)
        for child in widget.winfo_children():
            self._attach_tree(child)

    def _widget(self, widget):
        if widget is None:
            widget = self.root.focus_get()
        try:
            return widget if widget.winfo_class() in EDITABLE_CLASSES else None
        except (AttributeError, tk.TclError):
            return None

    @staticmethod
    def _is_text(widget):
        return widget.winfo_class() == "Text"

    @staticmethod
    def _disabled(widget):
        if str(widget.cget("state")) == "disabled":
            return True
        return hasattr(widget, "instate") and widget.instate(["disabled"])

    def _editable(self, widget):
        if self._disabled(widget) or str(widget.cget("state")) == "readonly":
            return False
        return not (hasattr(widget, "instate") and widget.instate(["readonly"]))

    def _masked(self, widget):
        return not self._is_text(widget) and bool(widget.cget("show"))

    def _selection(self, widget):
        if self._is_text(widget):
            ranges = widget.tag_ranges("sel")
            if not ranges:
                return None
            return ranges[0], ranges[-1]
        if widget.selection_present():
            return widget.index("sel.first"), widget.index("sel.last")
        return None

    def paste(self, widget=None) -> bool:
        widget = self._widget(widget)
        if widget is None:
            return False
        try:
            if not self._editable(widget):
                return False
            value = self.root.clipboard_get()
            if not value:
                return False
            selected = self._selection(widget)
            if selected:
                start, end = selected
                widget.delete(start, end)
                if self._is_text(widget):
                    widget.mark_set("insert", start)
                else:
                    widget.icursor(start)
            widget.insert("insert", value)
            if self._is_text(widget):
                widget.see("insert")
            return True
        except tk.TclError:
            # No textual clipboard contents, a destroyed widget, or unavailable
            # native clipboard should not interrupt the download form.
            return False

    def copy(self, widget=None) -> bool:
        widget = self._widget(widget)
        if widget is None:
            return False
        try:
            if self._disabled(widget) or self._masked(widget):
                return False
            selected = self._selection(widget)
            if not selected:
                return False
            if self._is_text(widget):
                value = widget.get(*selected)
            else:
                value = widget.get()[selected[0]:selected[1]]
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            return True
        except tk.TclError:
            return False

    def cut(self, widget=None) -> bool:
        widget = self._widget(widget)
        if widget is None:
            return False
        try:
            if not self._editable(widget) or self._masked(widget):
                return False
            selected = self._selection(widget)
            if not selected or not self.copy(widget):
                return False
            widget.delete(*selected)
            return True
        except tk.TclError:
            return False

    def select_all(self, widget=None) -> bool:
        widget = self._widget(widget)
        if widget is None:
            return False
        try:
            if self._disabled(widget):
                return False
            if self._is_text(widget):
                widget.tag_add("sel", "1.0", "end-1c")
                widget.mark_set("insert", "end-1c")
                widget.see("insert")
            else:
                widget.selection_range(0, "end")
                widget.icursor("end")
            return True
        except tk.TclError:
            return False

    def _populate_menu(self, menu, widget=None, accelerators=False):
        modifier = "⌘" if self.window_system == "aqua" else "Ctrl+"
        for label, key, operation in (("Вырезать", "X", self.cut), ("Копировать", "C", self.copy),
                                      ("Вставить", "V", self.paste)):
            menu.add_command(label=label, command=lambda operation=operation: operation(widget),
                             accelerator=modifier + key if accelerators else "")
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: self.select_all(widget),
                         accelerator=modifier + "A" if accelerators else "")

    def _update_menu(self, menu, widget=None):
        widget = self._widget(widget)
        try:
            selected = bool(widget and self._selection(widget))
            active = bool(widget and not self._disabled(widget))
            editable = bool(active and self._editable(widget))
            exportable = bool(active and selected and not self._masked(widget))
            for index, enabled in ((0, exportable and editable), (1, exportable),
                                   (2, editable), (4, active)):
                menu.entryconfigure(index, state="normal" if enabled else "disabled")
        except tk.TclError:
            pass

    def show_context_menu(self, event):
        widget = self._widget(event.widget)
        if widget is None:
            return None
        try:
            widget.focus_set()
            if self.context is not None:
                self.context.destroy()
            self.context = tk.Menu(self.root, tearoff=False)
            self._populate_menu(self.context, widget)
            self._update_menu(self.context, widget)
            try:
                self.context.tk_popup(event.x_root, event.y_root)
            finally:
                self.context.grab_release()
        except tk.TclError:
            pass
        return "break"

    def _install_edit_menu(self):
        existing = self.root.cget("menu")
        if existing:
            try:
                menubar = self.root.nametowidget(existing)
            except KeyError:
                return
        else:
            menubar = tk.Menu(self.root)
            self.root.configure(menu=menubar)
        self.edit_menu = tk.Menu(menubar, tearoff=False)
        self._populate_menu(self.edit_menu, accelerators=True)
        self.edit_menu.configure(postcommand=lambda: self._update_menu(self.edit_menu))
        menubar.add_cascade(label="Правка", menu=self.edit_menu)


def install_clipboard_support(root: tk.Tk) -> ClipboardSupport:
    """Install once per window; return editing helpers for a Paste button too."""
    existing = getattr(root, "_disk_downloader_clipboard", None)
    if existing is None:
        existing = ClipboardSupport(root)
        root._disk_downloader_clipboard = existing
    return existing
