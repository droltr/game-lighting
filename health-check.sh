#!/usr/bin/env bash
set -u

ok=0; warn=0; fail=0
check() { local state=$1 name=$2 detail=$3; printf '[%s] %s: %s\n' "$state" "$name" "$detail"; case "$state" in OK) ((ok+=1));; WARN) ((warn+=1));; ERROR) ((fail+=1));; esac; }

if systemctl --user is-active --quiet openrgb-server.service; then check OK openrgb-service active; else check ERROR openrgb-service inactive; fi
count=$(pgrep -cx openrgb 2>/dev/null || true)
if [[ "$count" == 1 ]]; then check OK openrgb-process "one process"; elif [[ "$count" == 0 ]]; then check ERROR openrgb-process "not running"; else check ERROR openrgb-process "$count processes"; fi

if timeout 2 bash -c '</dev/tcp/127.0.0.1/6742' 2>/dev/null; then check OK sdk-port listening; else check ERROR sdk-port unavailable; fi
if sensors -j >/dev/null 2>&1; then check OK cpu-sensor readable; else check ERROR cpu-sensor unavailable; fi
if systemctl --user is-active --quiet game-lighting.service; then check OK game-lighting-service active; else check ERROR game-lighting-service inactive; fi

if [[ -f /etc/udev/rules.d/60-openrgb.rules && -f /usr/lib/udev/rules.d/60-openrgb.rules ]]; then
  check ERROR udev-rules duplicate-owners
else
  check OK udev-rules single-owner
fi

if [[ "$fail" -gt 0 ]]; then exit 2; elif [[ "$warn" -gt 0 ]]; then exit 1; else exit 0; fi
