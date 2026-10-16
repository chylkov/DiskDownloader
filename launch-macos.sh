#!/bin/zsh
# Shared macOS entry point. Sourcing defines functions without launching anything.

disk_macos_candidates() {
  emulate -L zsh
  local project_directory="$1"
  local binary_directory
  reply=("$project_directory/.venv/bin/python3")
  for binary_directory in "${path[@]}"; do
    reply+=("${binary_directory:-.}/python3")
  done
  reply+=(/Library/Frameworks/Python.framework/Versions/3.*/bin/python3(NnOn))
  reply+=(/opt/homebrew/bin/python3 /usr/local/bin/python3)
}

disk_macos_managed_python() {
  print -r -- '/Library/Frameworks/Python.framework/Versions/3.14/bin/python3'
}

disk_macos_is_system_stub() {
  emulate -L zsh
  local physical_runtime="${1:A}"
  [[ "$physical_runtime" == /usr/bin/python3 || "$physical_runtime" == /usr/bin/python ]]
}

disk_macos_probe() {
  emulate -L zsh
  local runtime_candidate="$1"
  local mode="$2"
  [[ -f "$runtime_candidate" && -x "$runtime_candidate" ]] || return 1
  # Resolve aliases only to reject developer-tools stubs; invoke the original
  # venv path so Python can still discover its pyvenv.cfg.
  if disk_macos_is_system_stub "$runtime_candidate"; then
    return 1
  fi
  local probe_code='
import os
from pathlib import Path
import re
import ssl
import sys

if sys.version_info < (3, 10):
    sys.exit(1)
context = ssl.create_default_context()
if not context.cert_store_stats().get("x509_ca", 0):
    capath = ssl.get_default_verify_paths().capath
    for directory in (capath or "").split(os.pathsep):
        if not directory:
            continue
        try:
            for entry in Path(directory).iterdir():
                if not re.fullmatch(r"[0-9a-fA-F]{8}\.\d+", entry.name):
                    continue
                try:
                    context.load_verify_locations(cafile=str(entry))
                except (OSError, ssl.SSLError):
                    continue
                if context.cert_store_stats().get("x509_ca", 0):
                    break
        except OSError:
            continue
        if context.cert_store_stats().get("x509_ca", 0):
            break
if not context.cert_store_stats().get("x509_ca", 0):
    sys.exit(1)
if sys.argv[1] == "gui":
    import tkinter as tk
    for variable, folder, marker in (("TCL_LIBRARY", "tcl8.6", "init.tcl"),
                                      ("TK_LIBRARY", "tk8.6", "tk.tcl")):
        current = os.environ.get(variable)
        if current and (Path(current) / marker).is_file():
            continue
        for prefix in (Path(sys.prefix), Path(sys.base_prefix)):
            candidates = (prefix / "tcl" / folder, prefix / "lib" / folder)
            candidate = next((item for item in candidates if (item / marker).is_file()), None)
            if candidate is not None:
                os.environ[variable] = str(candidate)
                break
    window = tk.Tk()
    try:
        window.withdraw()
        window.update_idletasks()
        version = window.tk.call("package", "provide", "Tk")
        if int(window.tk.call("package", "vcompare", version, "8.6.13")) < 0:
            sys.exit(1)
    finally:
        window.destroy()
'
  "$runtime_candidate" -I -X utf8 -c "$probe_code" "$mode" >/dev/null 2>&1
}

disk_macos_launch() {
  emulate -L zsh
  local mode="$1"
  local project_directory="$2"
  shift 2
  local entry_script
  case "$mode" in
    gui) entry_script=disk_downloader_gui.py ;;
    cli) entry_script=disk_downloader.py ;;
    *) print -u2 -r -- 'Используйте run-gui.command для формы или run.command для консольного режима.'; return 1 ;;
  esac
  if ! cd -- "$project_directory"; then
    print -u2 -r -- 'Не удалось открыть папку DiskDownloader.'
    return 1
  fi
  project_directory="$PWD"
  if [[ ! -f "$project_directory/$entry_script" ]]; then
    print -u2 -r -- 'Не найден файл приложения. Распакуйте проект целиком и повторите запуск.'
    return 1
  fi
  local -a reply
  local -aU runtime_candidates
  disk_macos_candidates "$project_directory"
  runtime_candidates=("${reply[@]}")
  local runtime_candidate probe_status
  for runtime_candidate in "${runtime_candidates[@]}"; do
    disk_macos_probe "$runtime_candidate" "$mode"
    probe_status=$?
    if (( probe_status == 130 || probe_status == 143 )); then
      return "$probe_status"
    fi
    if (( probe_status == 0 )); then
      "$runtime_candidate" -X utf8 "$project_directory/$entry_script" "$@"
      return $?
    fi
  done
  local bootstrap_script="$project_directory/bootstrap-python-macos.sh"
  if [[ ! -f "$bootstrap_script" ]]; then
    print -u2 -r -- 'Не найден bootstrap-python-macos.sh. Распакуйте проект целиком и повторите запуск.'
    return 1
  fi
  print -r -- 'Подходящий Python не найден. Подготовка Python для DiskDownloader...'
  /bin/zsh "$bootstrap_script"
  local bootstrap_status=$?
  if (( bootstrap_status != 0 )); then
    return "$bootstrap_status"
  fi
  runtime_candidate="$(disk_macos_managed_python)"
  disk_macos_probe "$runtime_candidate" "$mode"
  probe_status=$?
  if (( probe_status != 0 )); then
    print -u2 -r -- 'После установки Python не прошёл проверку версии, сертификатов или Tk. Повторите запуск после проверки установки.'
    return "$probe_status"
  fi
  "$runtime_candidate" -X utf8 "$project_directory/$entry_script" "$@"
  return $?
}

if [[ "$ZSH_EVAL_CONTEXT" == toplevel ]]; then
  if (( $# < 1 )); then
    print -u2 -r -- 'Укажите режим запуска: gui или cli.'
    exit 1
  fi
  disk_macos_launch "$1" "${0:A:h}" "${@:2}"
  exit $?
fi
