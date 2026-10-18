#!/bin/sh
# Steer the relay NIC's queue interrupts to CPU 0, away from the relay's cores (compose cpuset 1,2).
iface=${1:-enp0s6}
cpus=${2:-0}
dev=$(basename "$(readlink "/sys/class/net/$iface/device")")
for irq in $(grep -E "$dev-(input|output)" /proc/interrupts | cut -d: -f1 | tr -d ' '); do
  echo "$cpus" > "/proc/irq/$irq/smp_affinity_list"
done
