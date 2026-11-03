#!/bin/bash
# llmt-pin.sh — commit and push a new llm-translators version whose only change is new translator pins.
#
#   llmt-pin.sh                 pin every translator whose branch has new pushed commits
#   llmt-pin.sh cfdt feat       pin only these
#   llmt-pin.sh --no-install …  don't reinstall the moved translators in this checkout afterwards
#
# Steps: refuse if anything is staged (the commit must hold the pins only); pull; move the pins to each
# translator's newest pushed commit, commit "Pin …" and push (llmt pin --commit --push); show what changed;
# reinstall the moved translators installed here (llmt update KEY --no-pull, so new dependencies arrive);
# print the commands for the servers. Repository: $LLMT_REPO or ~/Software/llm-translators.
set -euo pipefail

REPO="${LLMT_REPO:-$HOME/Software/llm-translators}"
INSTALL=1
KEYS=()
for a in "$@"; do
    case "$a" in
        --no-install) INSTALL=0 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        -*) echo "unknown option $a" >&2; exit 2 ;;
        *) KEYS+=("$a") ;;
    esac
done

cd "$REPO"

# 1. the commit must contain the pins only
if [ -n "$(git diff --cached --name-only)" ]; then
    echo "Staged changes in $REPO: the pin commit must contain the pins only. Commit or unstage them first:" >&2
    git diff --cached --stat >&2
    exit 1
fi
other=$(git status --porcelain --ignore-submodules=all | grep -v '^??' || true)
if [ -n "$other" ]; then
    echo "Note: uncommitted changes (not included in the pin commit):"
    echo "$other" | sed 's/^/  /'
fi

# 2. up to date with origin, then pin, commit and push
git pull --ff-only --quiet
before=$(git rev-parse HEAD)
./llmt pin ${KEYS[@]+"${KEYS[@]}"} --commit --push
after=$(git rev-parse HEAD)
if [ "$before" = "$after" ]; then
    echo "Nothing new to pin: no commit made."
    exit 0
fi

# 3. what changed: each moved translator with its newest commit subject (it starts with the version)
moved_paths=$(git diff --name-only "$before" "$after")
echo
echo "Committed and pushed $(git log -1 --format='%h "%s"' "$after"):"
for p in $moved_paths; do
    echo "  $p: $(git -C "$p" log -1 --format='%h %s' 2>/dev/null | cut -c1-110)"
done

# 4. reinstall here the moved translators that are installed in this checkout
moved_keys=$(python3 - "$moved_paths" <<'PY'
import json, sys
paths = set(sys.argv[1].split())
reg = json.load(open("translators.json"))
reg = reg.get("translators", reg)
try:
    installed = set(json.load(open("config/setup.json")).get("installed", []))
except (OSError, ValueError):
    installed = set()
print(" ".join(k for k, t in reg.items() if isinstance(t, dict) and t.get("repo") in paths and k in installed))
PY
)
if [ -n "$moved_keys" ]; then
    if [ "$INSTALL" = 1 ]; then
        echo
        echo "Reinstalling here (new dependencies): $moved_keys"
        # shellcheck disable=SC2086
        ./llmt update $moved_keys --no-pull --no-tune
    else
        echo
        echo "Installed here and moved: $moved_keys. Reinstall with: ./llmt update $moved_keys --no-pull"
    fi
fi

# 5. the servers
echo
echo "On each server (carbonio, mochi, spark1, spark2):"
echo "  cd /opt/llm-translators/src && sudo git pull && sudo git submodule update --init && sudo ./llmt deploy --test"
echo "Then restart the Claude app here (or wait: idle servers on the old release exit by themselves)."
