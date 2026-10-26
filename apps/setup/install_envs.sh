#!/bin/bash
# Build the isolated environments for the PAIR rebuild. Each part runs as its own
# background job and writes env/logs/<part>.log plus env/logs/<part>.status.
# The base interpreter ($PYTHON, Python 3.12) is only used to create the venvs.
#
# Usage: apps/setup/install_envs.sh [vllm|tools|oscar|binaries|all]
set -u
R=$(cd "$(dirname "$0")/../.." && pwd)
E=$R/env
PY=${PYTHON:-python3.12}
mkdir -p "$E/logs" "$E/downloads"

status() { echo "$2 $(date -u +%FT%TZ)" > "$E/logs/$1.status"; }

pip_install() {  # venv, then packages
  local venv=$1; shift
  "$venv/bin/python" -m pip install -q --upgrade pip &&
  "$venv/bin/python" -m pip install "$@"
}

part_vllm() {
  status vllm RUNNING
  $PY -m venv "$E/vllm" &&
  pip_install "$E/vllm" "vllm==0.31.0" "transformers==5.17.0" &&
  "$E/vllm/bin/python" -m pip freeze > "$E/logs/vllm.freeze.txt" &&
  "$E/vllm/bin/python" -c "import torch,transformers,vllm;print('torch',torch.__version__,'cuda',torch.version.cuda,'transformers',transformers.__version__,'vllm',vllm.__version__)" &&
  status vllm OK || status vllm FAILED
}

part_tools() {
  status tools RUNNING
  $PY -m venv "$E/tools" &&
  pip_install "$E/tools" "playwright==1.55.0" pytest jsonschema "PyMySQL==1.1.1" &&
  "$E/tools/bin/python" -m pip freeze > "$E/logs/tools.freeze.txt" &&
  export PLAYWRIGHT_BROWSERS_PATH="$E/browsers" &&
  "$E/tools/bin/python" -m playwright install chromium-headless-shell &&
  status tools OK || status tools FAILED
}

part_oscar() {
  status oscar RUNNING
  $PY -m venv "$E/oscar" &&
  pip_install "$E/oscar" "django-oscar==4.0" "django-treebeard==4.7.1" "Django>=4.2,<5.2" "sorl-thumbnail==12.11.0" "Whoosh==2.7.4" &&
  "$E/oscar/bin/python" -m pip freeze > "$E/logs/oscar.freeze.txt" &&
  status oscar OK || status oscar FAILED
}

part_binaries() {
  status binaries RUNNING
  local D=$E/downloads
  local MDB=mariadb-11.4.13 PHPV=8.2.32 SCRM=7.14.9
  cd "$D" &&
  curl -fsSL -o $MDB-linux-systemd-x86_64.tar.gz \
    https://archive.mariadb.org/$MDB/bintar-linux-systemd-x86_64/$MDB-linux-systemd-x86_64.tar.gz &&
  curl -fsSL -o php-$PHPV-cli-linux-x86_64.tar.gz \
    https://dl.static-php.dev/static-php-cli/bulk/php-$PHPV-cli-linux-x86_64.tar.gz &&
  curl -fsSL -o SuiteCRM-$SCRM.zip \
    https://github.com/salesagility/SuiteCRM/releases/download/v$SCRM/SuiteCRM-$SCRM.zip &&
  mkdir -p "$E/mariadb" "$E/php/bin" "$E/suitecrm-src" &&
  tar -xzf $MDB-linux-systemd-x86_64.tar.gz -C "$E/mariadb" --strip-components=1 &&
  tar -xzf php-$PHPV-cli-linux-x86_64.tar.gz -C "$E/php/bin" &&
  unzip -q -o SuiteCRM-$SCRM.zip -d "$E/suitecrm-src" &&
  ls -la "$D" > "$E/logs/binaries.files.txt" &&
  "$E/php/bin/php" -v >> "$E/logs/binaries.files.txt" &&
  "$E/php/bin/php" -m >> "$E/logs/binaries.files.txt" &&
  status binaries OK || status binaries FAILED
}

target=${1:-all}
for p in vllm tools oscar binaries; do
  if [ "$target" = all ] || [ "$target" = "$p" ]; then
    ( part_$p ) > "$E/logs/$p.log" 2>&1 &
    echo "started $p pid $!"
  fi
done
wait
