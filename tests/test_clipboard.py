"""Clipboard editing uses harmless fixtures, never the user's OS clipboard."""
from __future__ import annotations

import gc
import os
import sys
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

from yandex_disk.ui_clipboard import install_clipboard_support


class FakeEntry:
    def __init__(self, text="", *, state="normal", show="", widget_class="TEntry"):
        self.text = text
        self.state = state
        self.show = show
        self.widget_class = widget_class
        self.caret = len(text)
        self.selected = None
        self.tags = ("entry", widget_class, ".", "all")
        self.focus_set = Mock()

    def winfo_class(self):
        return self.widget_class

    def winfo_children(self):
        return []

    def bindtags(self, tags=None):
        if tags is not None:
            self.tags = tags
        return self.tags

    def cget(self, key):
        return {"state": self.state, "show": self.show}[key]

    def instate(self, states):
        return self.state in states

    def selection_present(self):
        return self.selected is not None

    def selection_range(self, start, end):
        self.selected = (int(start), len(self.text) if end == "end" else int(end))

    def index(self, index):
        if index == "sel.first":
            return self.selected[0]
        if index == "sel.last":
            return self.selected[1]
        if index in ("end", "insert"):
            return len(self.text) if index == "end" else self.caret
        return int(index)

    def delete(self, start, end):
        start, end = self.index(start), self.index(end)
        self.text = self.text[:start] + self.text[end:]
        self.selected = None
        self.caret = min(self.caret, len(self.text))

    def insert(self, where, value):
        where = self.index(where)
        self.text = self.text[:where] + value + self.text[where:]
        self.caret = where + len(value)

    def icursor(self, where):
        self.caret = self.index(where)

    def get(self):
        return self.text


class FakeRoot:
    def __init__(self, children):
        self.children = children
        self.tk = SimpleNamespace(call=lambda *args: "x11")
        self.class_bindings = {("TEntry", "<<Paste>>"): "original Tk paste"}
        self.bindings = {}
        self.clipboard_get = Mock(return_value="ТЕСТОВЫЙ_ТОКЕН")
        self.clipboard_clear = Mock()
        self.clipboard_append = Mock()

    def winfo_class(self):
        return "Tk"

    def winfo_children(self):
        return self.children

    def focus_get(self):
        return self.children[0] if self.children else None

    def bind_class(self, tag, sequence, operation):
        self.class_bindings[tag, sequence] = operation

    def bind_all(self, sequence, operation, add=None):
        self.bindings[sequence] = operation


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.entry = FakeEntry("prefixsuffix")
        self.root = FakeRoot([self.entry])
        self.support = install_clipboard_support(self.root)

    def test_selected_text_is_replaced_at_selection_start(self):
        self.entry.selection_range(0, 6)
        self.entry.icursor("end")
        self.assertTrue(self.support.paste(self.entry))
        self.assertEqual(self.entry.text, "ТЕСТОВЫЙ_ТОКЕНsuffix")
        self.assertEqual(self.entry.caret, len("ТЕСТОВЫЙ_ТОКЕН"))

    def test_inserts_at_caret_without_selection(self):
        self.entry.icursor(6)
        self.support.paste(self.entry)
        self.assertEqual(self.entry.text, "prefixТЕСТОВЫЙ_ТОКЕНsuffix")

    def test_platform_shortcuts_paste_once_and_stop_class_binding(self):
        for window_system in ("aqua", "win32", "x11"):
            root = FakeRoot([self.entry])
            root.tk.call = lambda *args: window_system
            with patch("yandex_disk.ui_clipboard.ClipboardSupport._install_edit_menu"):
                support = install_clipboard_support(root)
            sequences = ["<Control-v>", "<Control-V>", "<Control-Cyrillic_em>",
                         "<Control-Cyrillic_EM>", "<<Paste>>"]
            if window_system == "aqua":
                sequences += ["<Command-v>", "<Command-V>", "<Command-Cyrillic_em>", "<Command-Cyrillic_EM>"]
            else:
                self.assertNotIn((support.bindtag, "<Command-v>"), root.class_bindings)
            for sequence in sequences:
                with self.subTest(platform=window_system, sequence=sequence):
                    self.entry.text = ""
                    self.entry.caret = 0
                    root.clipboard_get.reset_mock()
                    handler = root.class_bindings[support.bindtag, sequence]
                    self.assertEqual(handler(SimpleNamespace(widget=self.entry)), "break")
                    self.assertEqual(self.entry.text, "ТЕСТОВЫЙ_ТОКЕН")
                    root.clipboard_get.assert_called_once_with()

    def test_keeps_original_tk_bindings_and_installs_once(self):
        self.assertEqual(self.root.class_bindings["TEntry", "<<Paste>>"], "original Tk paste")
        self.assertEqual(self.entry.tags, ("entry", self.support.bindtag, "TEntry", ".", "all"))
        self.assertIs(install_clipboard_support(self.root), self.support)
        self.support.attach(self.entry)
        self.assertEqual(self.entry.tags.count(self.support.bindtag), 1)

    def test_new_dialog_entry_is_attached_on_map(self):
        entry = FakeEntry(widget_class="Entry")
        self.root.bindings["<Map>"](SimpleNamespace(widget=entry))
        self.assertIn(self.support.bindtag, entry.tags)

    def test_readonly_and_disabled_fields_cannot_be_modified(self):
        for state in ("readonly", "disabled"):
            with self.subTest(state=state):
                self.entry.state = state
                self.entry.selection_range(0, 6)
                self.assertFalse(self.support.paste(self.entry))
                self.assertFalse(self.support.cut(self.entry))
                self.assertEqual(self.entry.text, "prefixsuffix")
        self.root.clipboard_get.assert_not_called()
        self.root.clipboard_clear.assert_not_called()

    def test_masked_token_can_be_pasted_but_never_copied_or_cut(self):
        self.entry.show = "•"
        self.support.select_all(self.entry)
        self.assertFalse(self.support.copy(self.entry))
        self.assertFalse(self.support.cut(self.entry))
        self.root.clipboard_clear.assert_not_called()
        self.root.clipboard_append.assert_not_called()
        self.assertTrue(self.support.paste(self.entry))
        self.assertEqual(self.entry.text, "ТЕСТОВЫЙ_ТОКЕН")

    def test_copy_and_cut_plain_selection(self):
        self.entry.selection_range(0, 6)
        self.assertTrue(self.support.copy(self.entry))
        self.root.clipboard_append.assert_called_with("prefix")
        self.assertTrue(self.support.cut(self.entry))
        self.assertEqual(self.entry.text, "suffix")

    def test_empty_or_unavailable_clipboard_keeps_selection(self):
        self.entry.selection_range(0, 6)
        for failure in (False, True):
            with self.subTest(unavailable=failure):
                self.root.clipboard_get.side_effect = tk.TclError("fixture unavailable") if failure else None
                self.root.clipboard_get.return_value = ""
                self.assertFalse(self.support.paste(self.entry))
                self.assertEqual(self.entry.text, "prefixsuffix")
                self.assertEqual(self.entry.selected, (0, 6))

    def test_context_menu_provides_paste_and_disables_token_export(self):
        self.entry.show = "•"
        self.entry.selection_range(0, 6)
        with patch("yandex_disk.ui_clipboard.tk.Menu") as factory:
            menu = factory.return_value
            result = self.support.show_context_menu(SimpleNamespace(widget=self.entry, x_root=4, y_root=5))
            self.assertEqual(result, "break")
            commands = {call.kwargs["label"]: call.kwargs["command"]
                        for call in menu.add_command.call_args_list}
            commands["Вставить"]()
            self.assertEqual(self.entry.text, "ТЕСТОВЫЙ_ТОКЕНsuffix")
            menu.entryconfigure.assert_any_call(0, state="disabled")
            menu.entryconfigure.assert_any_call(1, state="disabled")
            menu.entryconfigure.assert_any_call(2, state="normal")
            menu.tk_popup.assert_called_once_with(4, 5)
            menu.grab_release.assert_called_once_with()


@unittest.skipUnless(os.environ.get("DISK_DOWNLOADER_GUI_TESTS") == "1", "requires a desktop Tk session")
class NativeClipboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from disk_downloader_gui import configure_tk_runtime
        configure_tk_runtime()

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        # Destroyed Tk objects must be freed on the main thread, not by a later
        # garbage collection inside some worker thread (Tcl aborts the process).
        self.addCleanup(gc.collect)
        self.addCleanup(self.root.destroy)
        # Replace the Tcl command as well as its Python accessors so even Tk's
        # original class handlers cannot read or overwrite the OS clipboard.
        self.clipboard = "БЕЗОПАСНЫЙ_ТЕСТОВЫЙ_ТОКЕН"
        self.root.tk.call("rename", "clipboard", "fixture_original_clipboard")
        self.root.createcommand("clipboard", self.fixture_clipboard)
        self.entry = ttk.Entry(self.root, show="•")
        self.entry.pack()
        self.support = install_clipboard_support(self.root)

    def fixture_clipboard(self, command, *args):
        if command == "get":
            return self.clipboard
        if command == "clear":
            self.clipboard = ""
        elif command == "append":
            self.clipboard += str(args[-1])
        return ""

    def test_real_token_field_replaces_selection(self):
        self.entry.insert(0, "старый_токен")
        self.entry.selection_range(0, "end")
        self.assertTrue(self.support.paste(self.entry))
        self.assertEqual(self.entry.get(), "БЕЗОПАСНЫЙ_ТЕСТОВЫЙ_ТОКЕН")

    def check_paste_shortcut(self, sequence, **event_options):
        self.root.deiconify()
        self.root.update()
        self.entry.focus_force()
        self.root.update()
        self.entry.event_generate(sequence, **event_options)
        self.root.update()
        self.assertEqual(self.entry.get(), "БЕЗОПАСНЫЙ_ТЕСТОВЫЙ_ТОКЕН")

    @unittest.skipUnless(sys.platform == "darwin", "Command shortcuts are macOS-only")
    def test_real_cmd_v_pastes_exactly_once(self):
        self.check_paste_shortcut("<Command-v>")

    def test_real_ctrl_v_pastes_exactly_once(self):
        self.check_paste_shortcut("<Control-v>")

    def test_real_ctrl_v_with_russian_layout(self):
        if self.support.window_system == "win32":
            # Windows delivers the physical V key as VK_V even with a Russian
            # layout. Tk's synthetic X11 Cyrillic keysym instead goes through
            # VkKeyScanW and may have no keycode on the runner's active layout.
            # Exercise the real Windows key representation without changing
            # the user's layout; Cyrillic handlers are also checked above.
            self.check_paste_shortcut("<KeyPress>", keycode=0x56, state=0x4)
        else:
            self.check_paste_shortcut("<Control-Cyrillic_em>")

    @unittest.skipUnless(sys.platform == "darwin", "Command shortcuts are macOS-only")
    def test_real_cmd_v_with_russian_layout(self):
        self.check_paste_shortcut("<Command-Cyrillic_em>")

    def test_real_virtual_paste_and_disabled_state(self):
        self.entry.event_generate("<<Paste>>")
        self.assertEqual(self.entry.get(), self.clipboard)
        self.entry.state(["disabled"])
        self.entry.event_generate("<<Paste>>")
        self.assertEqual(self.entry.get(), self.clipboard)

    def test_real_text_selection_replacement(self):
        text = tk.Text(self.root)
        text.insert("1.0", "до\nстарое\nпосле")
        text.tag_add("sel", "2.0", "2.end")
        self.assertTrue(self.support.paste(text))
        self.assertEqual(text.get("1.0", "end-1c"), "до\nБЕЗОПАСНЫЙ_ТЕСТОВЫЙ_ТОКЕН\nпосле")


if __name__ == "__main__":
    unittest.main()
