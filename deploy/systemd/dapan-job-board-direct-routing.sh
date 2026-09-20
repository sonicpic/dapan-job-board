#!/usr/bin/env bash
set -euo pipefail

table=200
preference=1
app_uid=10001

remove_rule() {
  while ip -4 rule del pref "$preference" uidrange "$app_uid-$app_uid" table "$table" 2>/dev/null; do
    :
  done
}

if [[ "${1:-}" == "--remove" ]]; then
  remove_rule
  ip -4 route flush table "$table" 2>/dev/null || true
  ip -4 route flush cache
  exit 0
fi

# WSL mirrored networking exposes both the Clash TUN default route and the
# physical LAN default route. Select the first gateway outside fake-IP space.
physical_default=$(ip -4 route show default | awk '
  $1 == "default" && $3 !~ /^198\.18\./ && $5 != "eth0" { print; exit }
')
if [[ -z "$physical_default" ]]; then
  echo "No physical IPv4 default route found" >&2
  exit 1
fi

gateway=$(awk '{for (i=1;i<=NF;i++) if ($i=="via") {print $(i+1); exit}}' <<<"$physical_default")
device=$(awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}' <<<"$physical_default")
source_address=$(ip -o -4 addr show dev "$device" scope global | awk 'NR==1 {split($4,a,"/"); print a[1]}')
connected_prefix=$(ip -4 route show dev "$device" scope link | awk '$1 ~ /\// {print $1; exit}')

if [[ -z "$gateway" || -z "$device" || -z "$source_address" || -z "$connected_prefix" ]]; then
  echo "Physical route details are incomplete" >&2
  exit 1
fi

remove_rule
ip -4 route flush table "$table" 2>/dev/null || true
ip -4 route replace table "$table" "$connected_prefix" dev "$device" src "$source_address"
ip -4 route replace table "$table" default via "$gateway" dev "$device" src "$source_address"

# Preserve WSL mirrored localhost routing so Windows FRPC can still reach the
# application through 127.0.0.1 while public traffic uses the physical gateway.
while IFS= read -r route; do
  [[ -n "$route" ]] && ip -4 route replace table "$table" $route
done < <(ip -4 route show table 127)

ip -4 rule add pref "$preference" uidrange "$app_uid-$app_uid" table "$table"
ip -4 route flush cache
