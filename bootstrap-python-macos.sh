#!/bin/zsh
# Official universal2 installer; no downloads or installation when sourced.
# Pins: https://www.python.org/downloads/release/python-3148/

disk_bootstrap_python_path() {
  print -r -- '/Library/Frameworks/Python.framework/Versions/3.14/bin/python3'
}

disk_bootstrap_applications_path() {
  print -r -- '/Applications/Python 3.14'
}

disk_bootstrap_supported() {
  local version architecture
  version=$(/usr/bin/sw_vers -productVersion 2>/dev/null) || return 1
  architecture=$(/usr/bin/uname -m 2>/dev/null) || return 1
  [[ "$architecture" == arm64 || "$architecture" == x86_64 ]] || return 1
  local -a numbers=("${(@s:.:)version}")
  [[ "${numbers[1]:-}" == <-> && "${numbers[2]:-}" == <-> ]] || return 1
  (( numbers[1] > 10 || (numbers[1] == 10 && numbers[2] >= 15) ))
}

disk_bootstrap_runtime_ok() {
  local python_path="$(disk_bootstrap_python_path)"
  [[ -x "$python_path" ]] || return 1
  "$python_path" -I -X utf8 -c '
import re, ssl, sys
if sys.version_info < (3, 10):
    sys.exit(1)
context = ssl.create_default_context()
if not context.cert_store_stats()["x509_ca"]:
    sys.exit(1)
import tkinter as tk
window = tk.Tk()
window.withdraw()
try:
    version = tuple(int(n) for n in re.findall(r"\d+", str(window.tk.call("package", "provide", "Tk")))[:3])
    if version < (8, 6, 13):
        sys.exit(1)
finally:
    window.destroy()
' >/dev/null 2>&1
}

disk_bootstrap_conflict() {
  local python_path="$(disk_bootstrap_python_path)"
  local framework_directory="${python_path:h:h}"
  local applications="$(disk_bootstrap_applications_path)"
  [[ -e "$framework_directory" || -L "$framework_directory" || -e "$applications" || -L "$applications" ]]
}

disk_bootstrap_consent() {
  local answer
  read -r 'answer?Установить Python с Tkinter? [Y/Д = да, Enter = нет]: ' || return 1
  [[ "${answer:l}" == y || "${answer:l}" == yes || "${answer:l}" == д || "${answer:l}" == да ]]
}

disk_bootstrap_download() {
  /usr/bin/curl --fail --silent --show-error --location --max-redirs 0 \
    --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --connect-timeout 30 --max-time 300 --retry 2 \
    --output "$1" \
    'https://www.python.org/ftp/python/3.14.8/python-3.14.8-macos11.pkg'
}

disk_bootstrap_signature() {
  LC_ALL=C /usr/sbin/pkgutil --check-signature "$1" 2>/dev/null
}

disk_bootstrap_gatekeeper() {
  /usr/sbin/spctl --assess --type install "$1" >/dev/null 2>&1
}

disk_bootstrap_expected_hash() {
  print -r -- '507fc086c5c006ff875d344a75b4e67b8fb3c401f1bc4908c6250adb673d4907'
}

disk_bootstrap_verify() {
  local package="$1" digest signature line found=0
  digest=$(/usr/bin/shasum -a 256 -- "$package" 2>/dev/null) || return 1
  [[ "${digest%% *}" == "$(disk_bootstrap_expected_hash)" ]] || return 1
  signature=$(disk_bootstrap_signature "$package") || return 1
  for line in "${(@f)signature}"; do
    if [[ "$line" =~ '^[[:space:]]*1[.][[:space:]]+Developer ID Installer: Python Software Foundation [(]BMM5U3QVKW[)][[:space:]]*$' ]]; then
      found=1
    fi
  done
  (( found )) || return 1
  disk_bootstrap_gatekeeper "$package"
}

disk_bootstrap_install() {
  local package="$1" choices="$2"
  # Native macOS authorization owns the password prompt. Never read a password
  # in the terminal or interpolate paths into AppleScript source.
  /usr/bin/osascript - "$package" "$choices" >/dev/null 2>&1 <<'APPLESCRIPT'
on run argv
    set packageFile to item 1 of argv
    set choicesFile to item 2 of argv
    set frameworkDir to "/Library/Frameworks/Python.framework/Versions/3.14"
    set applicationsDir to "/Applications/Python 3.14"
    set guard to "/bin/test ! -e " & quoted form of frameworkDir & " && /bin/test ! -L " & quoted form of frameworkDir & " && /bin/test ! -e " & quoted form of applicationsDir & " && /bin/test ! -L " & quoted form of applicationsDir
    set installCommand to guard & " && /usr/sbin/installer -pkg " & quoted form of packageFile & " -applyChoiceChangesXML " & quoted form of choicesFile & " -target /"
    do shell script installCommand with administrator privileges
end run
APPLESCRIPT
}

disk_bootstrap_certificates() {
  local applications="$(disk_bootstrap_applications_path)"
  [[ -f "$applications/Install Certificates.command" ]] || return 1
  # The PSF certificate setup downloads certifi from PyPI. Run as the current
  # user, with no inherited pip configuration or project directory on sys.path.
  # In particular, never run pip with administrator privileges.
  (
    cd /private/var/empty || return 1
    /usr/bin/env -i HOME="$HOME" PATH='/usr/bin:/bin:/usr/sbin:/sbin' \
      PIP_CONFIG_FILE=/dev/null PIP_INDEX_URL='https://pypi.org/simple' \
      PIP_ONLY_BINARY=':all:' PIP_NO_DEPS=1 PIP_NO_INPUT=1 \
      PIP_DEFAULT_TIMEOUT=30 PIP_RETRIES=2 \
      /bin/sh "$applications/Install Certificates.command"
  ) >/dev/null 2>&1
}

disk_bootstrap_choices() {
  # Use absolute framework paths; preserve existing shell profiles and commands.
  /bin/cat > "$1" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><array>
<dict><key>choiceIdentifier</key><string>org.python.Python.PythonUnixTools-3.14</string><key>choiceAttribute</key><string>selected</string><key>attributeSetting</key><integer>0</integer></dict>
<dict><key>choiceIdentifier</key><string>org.python.Python.PythonProfileChanges-3.14</string><key>choiceAttribute</key><string>selected</string><key>attributeSetting</key><integer>0</integer></dict>
</array></plist>
PLIST
}

disk_bootstrap_main() {
  emulate -L zsh
  local temporary_directory='' package choices temporary_root="${TMPDIR:-/tmp}"
  {
    if ! disk_bootstrap_supported; then
      print 'Автоматическая установка требует macOS 10.15 или новее на Intel или Apple Silicon.'
      return 1
    fi
    if disk_bootstrap_runtime_ok; then
      print 'Python с Tkinter уже готов к запуску DiskDownloader.'
      return 0
    fi
    if disk_bootstrap_conflict; then
      print 'Python 3.14 уже установлен, но не прошёл проверку Python, Tkinter или сертификатов HTTPS.'
      print 'Запустите /Applications/Python 3.14/Install Certificates.command или восстановите Python вручную с https://www.python.org/downloads/macos/ .'
      return 1
    fi
    print 'Будет установлен Python 3.14.8 с Tkinter с официального сайта python.org.'
    print 'Понадобятся интернет и пароль администратора в системном окне macOS.'
    print 'Python установится в /Library/Frameworks; затем настроятся сертификаты HTTPS (PyPI).'
    if ! disk_bootstrap_consent; then
      print 'Установка отменена. Python не скачивался.'
      return 2
    fi
    temporary_directory=$(/usr/bin/mktemp -d "${temporary_root%/}/disk-downloader-python.XXXXXXXX") || {
      print 'Не удалось создать временную папку для установщика Python.'
      return 1
    }
    package="$temporary_directory/python-installer.pkg"
    choices="$temporary_directory/choices.plist"
    print 'Скачивание установщика Python...'
    if ! disk_bootstrap_download "$package"; then
      print 'Не удалось скачать Python. Проверьте интернет и повторите запуск.'
      return 1
    fi
    print 'Проверка установщика Python...'
    if ! disk_bootstrap_verify "$package"; then
      print 'Установщик не прошёл проверку SHA-256, подписи Python Software Foundation или Gatekeeper. Установка не запускалась.'
      return 1
    fi
    if disk_bootstrap_conflict; then
      print 'Обнаружена другая установка Python 3.14. Повторите запуск после её завершения.'
      return 1
    fi
    disk_bootstrap_choices "$choices" || return 1
    print 'Установка Python. Подтвердите запрос macOS...'
    if ! disk_bootstrap_install "$package" "$choices"; then
      print 'Установка Python не завершена или запрос администратора отменён.'
      return 1
    fi
    print 'Настройка сертификатов HTTPS...'
    if ! disk_bootstrap_certificates; then
      print 'Python установлен, но сертификаты HTTPS не настроены. Проверьте интернет и запустите /Applications/Python 3.14/Install Certificates.command вручную.'
      return 1
    fi
    if ! disk_bootstrap_runtime_ok; then
      print 'Python установлен, но проверка Tkinter или HTTPS не прошла. Проверьте установку Python и повторите запуск.'
      return 1
    fi
    print 'Python с Tkinter установлен и проверен. Запускаем DiskDownloader.'
    return 0
  } always {
    if [[ -n "$temporary_directory" && -d "$temporary_directory" ]]; then
      /bin/rm -rf -- "$temporary_directory"
    fi
  }
}

if [[ "$ZSH_EVAL_CONTEXT" == toplevel ]]; then
  disk_bootstrap_main
  exit $?
fi
