# Sourced by bin/run, bin/mcp and bin/activate (needs _llmt_root set): loads the settings in order
#   1. the machine:   ${LLMT_SYSTEM_ENV:-/etc/llm-translators/env}          (admin)
#   2. the user:      a shared release: ${XDG_CONFIG_HOME:-~/.config}/llm-translators/env
#                     a personal checkout: <checkout>/config/env
#   3. (a shared release) the machine's *_MAX_* caps again, so a user cannot raise them
# Prints nothing. docs/SHARED.md.
_llmt_sys=${LLMT_SYSTEM_ENV:-/etc/llm-translators/env}
if [ -f "$_llmt_root/RELEASE.json" ]; then
    _llmt_usr=${XDG_CONFIG_HOME:-$HOME/.config}/llm-translators/env
else
    _llmt_usr=$_llmt_root/config/env
fi
set -a
[ -r "$_llmt_sys" ] && . "$_llmt_sys"
[ -r "$_llmt_usr" ] && . "$_llmt_usr"
if [ -f "$_llmt_root/RELEASE.json" ] && [ -r "$_llmt_sys" ]; then
    eval "$(grep -E '^[[:space:]]*(export[[:space:]]+)?[A-Za-z0-9_]+_MAX_[A-Za-z0-9_]*=' "$_llmt_sys")"
fi
set +a
unset _llmt_sys _llmt_usr
