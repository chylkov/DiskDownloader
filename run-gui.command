#!/bin/zsh
cd -- "${0:A:h}" || exit 1
/bin/zsh "$PWD/launch-macos.sh" gui "$@"
result=$?
if (( result != 0 )); then
  print
  read '?Нажмите Enter, чтобы закрыть окно...'
fi
exit "$result"
