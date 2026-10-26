# Source this file: sets paths for the PAIR rebuild tools (browser libraries, browsers, runtime dir).
R=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)
export PAIR_ROOT=$R
export PAIR_RT=/tmp/pair_rebuild_rt
export PLAYWRIGHT_BROWSERS_PATH=$R/env/browsers
export LD_LIBRARY_PATH=$R/env/syslibs/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export PYTHONPATH=$R
