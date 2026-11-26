#!/usr/bin/env bash
# Install (or update) the boardctrl client on a workstation / build server.
#
#   deploy/install-client.sh <user@host> [DIR]       default DIR: ~/boardctrl
#   deploy/install-client.sh local [DIR]
#   ... --site <site-dir>    also ship a site directory to ~/.config/boardctrl/site
#                            (minus .git and the patterns in <site>/boardctrl-ignore)
#
# Ships the git-tracked tree (clean HEAD unless --allow-dirty) and links
# ~/.local/bin/boardctrl -> DIR/bin/boardctrl. Needs python3 >= 3.8 (+ PyYAML for `boardctrl sdk`).
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET=${1:?usage: deploy/install-client.sh <user@host|local> [DIR] [--allow-dirty]}
DIR=${2:-'~/boardctrl'}
[[ $DIR == --* ]] && DIR='~/boardctrl'
SITE=""
args=("$@"); for i in "${!args[@]}"; do [[ ${args[$i]} == --site ]] && SITE=${args[$((i+1))]}; done
REV=$(git rev-parse --short HEAD)
if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
  [[ " $* " == *" --allow-dirty "* ]] || { echo "uncommitted changes; commit or pass --allow-dirty" >&2; exit 1; }
  REV="$REV-dirty"
fi
INSTALL="set -e; D=$DIR; mkdir -p \$D; tar -xzf - -C \$D; echo $REV > \$D/REVISION;
  mkdir -p ~/.local/bin; ln -sf \$D/bin/boardctrl ~/.local/bin/boardctrl;
  python3 -c 'import yaml' 2>/dev/null || echo 'note: PyYAML missing: boardctrl sdk needs it (pip install --user pyyaml)';
  echo \"boardctrl client $REV installed in \$D (~/.local/bin/boardctrl)\""
if [[ $TARGET == local ]]; then
  git ls-files -z | tar --null -czf - -T - | bash -c "$INSTALL"
else
  git ls-files -z | tar --null -czf - -T - | ssh -o BatchMode=yes "$TARGET" "$INSTALL"
fi
if [[ -n $SITE ]]; then
  # never replace a site that is a symlink or a git checkout: that would drop its history
  SITE_INSTALL='set -e; T=~/.config/boardctrl/site
    if [ -L "$T" ] || [ -d "$T/.git" ]; then echo "refusing to replace $T: it is a symlink or git checkout (update it with git, or point BOARDCTRL_SITE at it)" >&2; exit 1; fi
    mkdir -p ~/.config/boardctrl; rm -rf ~/.config/boardctrl/site.new; mkdir ~/.config/boardctrl/site.new;
    tar -xzf - -C ~/.config/boardctrl/site.new; rm -rf ~/.config/boardctrl/site; mv ~/.config/boardctrl/site.new ~/.config/boardctrl/site;
    echo "site installed in ~/.config/boardctrl/site"'
  EXCL=(--exclude=.git); [[ -f $SITE/boardctrl-ignore ]] && EXCL+=(--exclude-from="$SITE/boardctrl-ignore")
  if [[ $TARGET == local ]]; then tar -C "$SITE" "${EXCL[@]}" -czf - . | bash -c "$SITE_INSTALL"
  else tar -C "$SITE" "${EXCL[@]}" -czf - . | ssh -o BatchMode=yes "$TARGET" "$SITE_INSTALL"; fi
fi
