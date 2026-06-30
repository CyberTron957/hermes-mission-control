#!/usr/bin/env bash
#
# release-dryrun.sh — rehearse an existing-user upgrade WITHOUT touching the real
# GitHub remote or your live server. Spins up a local "fake GitHub" bare repo, a
# clone installed as the OLD release in its own venv, and a throwaway data dir on
# a spare port. You then "mock push" the new release to fake-GitHub and run the
# old build's startup auto-update against it — exactly what a real user's
# `git pull` + reinstall + re-exec does.
#
# Nothing here can reach real GitHub (the clone's origin is the local bare repo)
# or your real data/port (dedicated DEMO dir + PORT).
#
# USAGE
#   scripts/release-dryrun.sh setup <OLD_REF> <NEW_REF>   # build the sandbox
#   scripts/release-dryrun.sh start                       # run the (old) server -d
#   scripts/release-dryrun.sh stop
#   scripts/release-dryrun.sh push                        # mock-push NEW_REF -> fake-github main
#   scripts/release-dryrun.sh health                      # curl /health
#   scripts/release-dryrun.sh oldcmd  <args...>           # run the OLD console cmd (e.g. status)
#   scripts/release-dryrun.sh newcmd  <args...>           # run the NEW console cmd (post-upgrade)
#   scripts/release-dryrun.sh log                         # tail the server log
#   scripts/release-dryrun.sh clean                       # delete the whole sandbox
#
# TYPICAL FLOW
#   scripts/release-dryrun.sh setup v0.4.2 name_change
#   scripts/release-dryrun.sh start        # old version boots
#   scripts/release-dryrun.sh health       #   confirm it's the OLD version + data
#   scripts/release-dryrun.sh stop
#   scripts/release-dryrun.sh push         # "release" the new code to fake-github
#   scripts/release-dryrun.sh start        # auto-update fires: pull+reinstall+re-exec
#   scripts/release-dryrun.sh oldcmd status   # old command still works (deprecation)
#   scripts/release-dryrun.sh newcmd status   # new command now exists
#   scripts/release-dryrun.sh health          # data survived the upgrade
#   scripts/release-dryrun.sh clean
#
# OVERRIDABLE ENV
#   DEMO   sandbox dir            (default: /tmp/teams-demo)
#   PORT   server port            (default: 8771)
#   REPO   source repo to test    (default: this script's git repo)
#   OLD_CMD / NEW_CMD  console-script names (default: hermes-swarm / agent-teams)
#   BORROW_SITE_PACKAGES  path to an existing venv's site-packages whose deps
#                         (fastapi/uvicorn/hermes/…) the demo venv should reuse
#                         instead of pip-installing them. Empty = full install.
#                         (default: autodetected from this shell's `python3`)

set -euo pipefail

DEMO="${DEMO:-/tmp/teams-demo}"
PORT="${PORT:-8771}"
OLD_CMD="${OLD_CMD:-hermes-swarm}"
NEW_CMD="${NEW_CMD:-agent-teams}"

REPO="${REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)}"

FGH="$DEMO/fake-github.git"      # the local stand-in for GitHub
INSTALL="$DEMO/install"          # the existing-user clone
VENV="$DEMO/venv"                # the existing-user venv
DATA="$DEMO/data"                # throwaway data dir
LOG="$DEMO/server.log"
META="$DEMO/refs.env"            # remembers OLD_REF/NEW_REF between subcommands

PY="$VENV/bin/python"

die() { echo "✗ $*" >&2; exit 1; }

# Hard safety rail: never operate on a port a real deployment is likely on.
# (A past bug let the OLD version fall back to its default 8000 and the stop path
# killed the real server. demo_env now sets both prefixes; this is belt-and-braces.)
case "$PORT" in 8000|80|443) die "refusing PORT=$PORT — pick a spare port (default 8771) to protect real servers";; esac

# Server isolation: spare port + demo data dir + auto-update OFF for plain
# start/stop (we trigger the update explicitly via `push` then `start`).
# CRITICAL: the sandbox spans BOTH versions, which read DIFFERENT env prefixes —
# the OLD release reads SWARM_*, the NEW one reads TEAMS_*. We MUST set both, or
# the running version silently falls back to its DEFAULT port (8000) and the
# stop/clean path then kills whatever real server is on 8000.
demo_env() {
  echo "SWARM_PORT=$PORT SWARM_HOST=127.0.0.1 SWARM_DATA_DIR=$DATA" \
       "TEAMS_PORT=$PORT TEAMS_HOST=127.0.0.1 TEAMS_DATA_DIR=$DATA"
}

die() { echo "✗ $*" >&2; exit 1; }

cmd_setup() {
  local old="${1:-}" new="${2:-}"
  [ -n "$old" ] && [ -n "$new" ] || die "usage: setup <OLD_REF> <NEW_REF>"
  git -C "$REPO" rev-parse --verify -q "$old" >/dev/null || die "OLD_REF '$old' not found in $REPO"
  git -C "$REPO" rev-parse --verify -q "$new" >/dev/null || die "NEW_REF '$new' not found in $REPO"
  git -C "$REPO" merge-base --is-ancestor "$old" "$new" \
    || die "'$old' is not an ancestor of '$new' — a real `git pull --ff-only` would reject this"

  echo "→ wiping any previous sandbox at $DEMO"
  cmd_clean_quiet
  mkdir -p "$DEMO"

  echo "→ fake GitHub (bare repo) seeded at OLD_REF=$old"
  git init -q --bare "$FGH"
  git -C "$REPO" push -q "$FGH" "$old:refs/heads/main"
  git --git-dir="$FGH" symbolic-ref HEAD refs/heads/main

  echo "→ existing-user clone (old code)"
  git clone -q "$FGH" "$INSTALL"

  echo "→ existing-user venv + OLD release install"
  python3 -m venv "$VENV"
  local borrow="${BORROW_SITE_PACKAGES-__auto__}"
  if [ "$borrow" = "__auto__" ]; then
    borrow="$(python3 -c 'import site,sys; print(site.getsitepackages()[0])' 2>/dev/null || true)"
  fi
  if [ -n "$borrow" ] && [ -d "$borrow" ]; then
    echo "  borrowing deps from: $borrow"
    local sp; sp="$(echo "$VENV"/lib/python*/site-packages)"
    echo "$borrow" > "$sp/zz_borrow_deps.pth"
    ( cd "$INSTALL" && "$PY" -m pip install -q --no-deps -e . )
  else
    ( cd "$INSTALL" && "$PY" -m pip install -q -e . )
  fi

  echo "→ seeding fake pre-existing user data (to prove it survives)"
  mkdir -p "$DATA"
  printf '%s\n' '{"agents":{"Alice":{"team_id":"acme"}},"_marker":"PRE-EXISTING DATA"}' \
    > "$DATA/agents_config.json"

  printf 'OLD_REF=%s\nNEW_REF=%s\n' "$old" "$new" > "$META"

  echo
  echo "✓ sandbox ready (OLD=$old  NEW=$new)"
  echo "  clone HEAD : $(git -C "$INSTALL" log --oneline -1)"
  echo "  command    : $(ls "$VENV/bin" | grep -E "^($OLD_CMD|$NEW_CMD)\$" | tr '\n' ' ')"
  echo "  port/data  : $PORT  /  $DATA"
  echo
  echo "next: $0 start   →   $0 stop   →   $0 push   →   $0 start"
}

cmd_start() {
  [ -x "$PY" ] || die "no sandbox — run: $0 setup <OLD_REF> <NEW_REF>"
  # Use the OLD command via the venv so the auto-update (git pull + reinstall +
  # re-exec) runs exactly as it would for a real user. AUTO_UPDATE stays ON.
  ( cd "$INSTALL" && env $(demo_env) "$VENV/bin/$OLD_CMD" up -d --log "$LOG" )
  sleep 5
  cmd_health || true
}

cmd_stop() {
  [ -x "$PY" ] || die "no sandbox"
  ( cd "$INSTALL" && env $(demo_env) "$VENV/bin/$OLD_CMD" down ) || true
}

cmd_push() {
  [ -f "$META" ] || die "no sandbox — run setup first"
  # shellcheck disable=SC1090
  . "$META"
  echo "→ mock-pushing NEW_REF=$NEW_REF to fake-github main (NOT real GitHub)"
  git -C "$REPO" push "$FGH" "$NEW_REF:main"
  echo "✓ fake-github main now at: $(git --git-dir="$FGH" log --oneline -1 main)"
  echo "  run '$0 start' to trigger the old build's startup auto-update."
}

cmd_health() { curl -sS -m4 "http://127.0.0.1:$PORT/health" && echo; }
cmd_oldcmd() { ( cd "$INSTALL" && env $(demo_env) "$VENV/bin/$OLD_CMD" "$@" ); }
cmd_newcmd() { ( cd "$INSTALL" && env $(demo_env) "$VENV/bin/$NEW_CMD" "$@" ); }
cmd_log()    { tail -n "${1:-40}" "$LOG"; }

cmd_clean_quiet() {
  if [ -x "$PY" ]; then ( cd "$INSTALL" 2>/dev/null && env $(demo_env) "$VENV/bin/$OLD_CMD" down >/dev/null 2>&1 ) || true; fi
  local pid; pid="$(lsof -ti "tcp:$PORT" 2>/dev/null || true)"; [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  rm -rf "$DEMO"
}
cmd_clean() { cmd_clean_quiet; echo "✓ removed $DEMO"; }

case "${1:-}" in
  setup)  shift; cmd_setup "$@";;
  start)  cmd_start;;
  stop)   cmd_stop;;
  push)   cmd_push;;
  health) cmd_health;;
  oldcmd) shift; cmd_oldcmd "$@";;
  newcmd) shift; cmd_newcmd "$@";;
  log)    shift; cmd_log "$@";;
  clean)  cmd_clean;;
  *) sed -n '2,60p' "$0" | sed 's/^# \?//'; exit 1;;
esac
