#!/bin/zsh
cd -- "${0:A:h}" || exit 1
/bin/zsh "$PWD/launch-macos.sh" cli "$@"
result=$?
print
read '?Нажмите Enter, чтобы закрыть окно...'
exit "$result"
