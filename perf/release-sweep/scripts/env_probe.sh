#!/usr/bin/env bash
# env_probe.sh — print the environment facts recorded in env-freeze.md.
# Run on the benchmark host; output is pasted (or diffed) into env-freeze.md.
#   ./env_probe.sh | tee ../../../../logs/env-freeze-raw.txt
set -uo pipefail

p() { printf '%-28s %s\n' "$1" "$2"; }

echo "== identity =="
p "date" "$(date -Is)"
p "hostname" "$(hostname)"
p "uname" "$(uname -r)"
p "distro" "$(. /etc/os-release && echo "$PRETTY_NAME")"
p "wsl" "$(wsl.exe -l -v 2>/dev/null | tr -d '\0' | tr -s ' \n' ' ' )"
p "kernel_cmdline" "$(cat /proc/cmdline)"

echo "== cpu =="
p "model" "$(lscpu | sed -n 's/^Model name: *//p')"
p "vendor" "$(lscpu | sed -n 's/^Vendor ID: *//p')"
p "logical_cpus" "$(nproc)"
p "topology" "$(lscpu | sed -n 's/^\(Socket(s)\|Core(s) per socket\|Thread(s) per core\|NUMA node(s)\):/  \1:/p' | tr -s ' ' | tr '\n' ';')"
p "caches" "$(lscpu | sed -n 's/^\(L1d\|L2\|L3\) cache: */\1=/p' | tr '\n' ' ')"
p "flags_of_interest" "$(lscpu | sed -n 's/^Flags: *//p' | tr ' ' '\n' | grep -E '^(avx2|avx512f|avx_vnni|aes|sha_ni)$' | tr '\n' ' ')"
p "march_native" "$(gcc -march=native -Q --help=target 2>/dev/null | sed -n 's/^  -march= *//p')"
p "thp" "$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null)"
p "governor" "$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo 'not exposed (WSL)')"
p "no_turbo" "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null || echo 'not exposed (WSL)')"

echo "== memory/disk =="
p "mem_total" "$(free -g | awk '/Mem:/{print $2" GiB"}')"
p "mem_available" "$(free -g | awk '/Mem:/{print $7" GiB"}')"
p "swap" "$(free -g | awk '/Swap:/{print $2" GiB"}')"
p "disk_home_free" "$(df -h /home | tail -1 | awk '{print $4}')"
p "hugepages" "$(grep -E 'HugePages_Total|Hugepagesize' /proc/meminfo | tr -s ' ' | tr '\n' ';')"

echo "== toolchain =="
p "gcc" "$(gcc --version 2>/dev/null | head -1)"
p "cmake" "$(cmake --version 2>/dev/null | head -1)"
p "make" "$(make --version 2>/dev/null | head -1)"
p "ninja" "$(ninja --version 2>/dev/null || echo absent)"
p "binutils" "$(ld --version 2>/dev/null | head -1)"
p "openssl" "$(openssl version 2>/dev/null)"
p "openssl_dir" "$(openssl version -d 2>/dev/null)"
p "python3" "$(python3 -V 2>&1) at $(command -v python3)"
p "python3.12" "$(/usr/bin/python3.12 -V 2>&1)"
p "arm_python" "$(/home/gmarzot/.local/share/uv/python/cpython-3.14.6-linux-x86_64-gnu/bin/python3.14 -V 2>&1)"
p "uv" "$(uv --version 2>/dev/null)"
p "jemalloc_lib" "$(ldconfig -p 2>/dev/null | grep -i jemalloc | head -3 | tr -s ' ' | tr '\n' ';')"
p "jemalloc_pkg" "$(dpkg -l 2>/dev/null | grep -i jemalloc | awk '{print $2"="$3}' | tr '\n' ';')"
p "cython_in_arm" "$(arms/base-070rc2/venv/bin/python -c 'import Cython; print(Cython.__version__)' 2>/dev/null || echo 'n/a')"

echo "== net: interface =="
IFACE=${IFACE:-eth1}
p "iface" "$IFACE"
p "ipv4" "$(ip -4 -br addr show "$IFACE" | awk '{print $3}')"
p "mtu" "$(cat "/sys/class/net/$IFACE/mtu")"
p "speed_mbps" "$(cat "/sys/class/net/$IFACE/speed" 2>/dev/null || echo 'not reported')"
p "duplex" "$(cat "/sys/class/net/$IFACE/duplex" 2>/dev/null || echo 'not reported')"
p "rx_tx_queues" "$(ls -d /sys/class/net/$IFACE/queues/rx-* 2>/dev/null | wc -l) rx / $(ls -d /sys/class/net/$IFACE/queues/tx-* 2>/dev/null | wc -l) tx"
p "txqueuelen" "$(cat "/sys/class/net/$IFACE/tx_queue_len")"
p "root_qdisc" "$(ip -d link show "$IFACE" | sed -n 's/.*qdisc \([a-z_]*\).*/\1/p' | head -1)"
p "child_qdiscs" "$(tc qdisc show dev "$IFACE" | grep -c fq_codel) x fq_codel"
p "rps_cpus" "$(for q in /sys/class/net/$IFACE/queues/rx-*/rps_cpus; do cat "$q"; done | sort -u | tr '\n' ' ')"
p "coalesce" "$(ethtool -c "$IFACE" 2>/dev/null | sed -n 's/^\(rx-usecs\|tx-usecs\): */\1=/p' | tr '\n' ' ' || echo 'ethtool unavailable')"
p "rings" "$(ethtool -g "$IFACE" 2>/dev/null | sed -n 's/^\(RX\|TX\): */\1=/p' | tr '\n' ' ' || echo 'ethtool unavailable')"

echo "== net: sysctls (actual) =="
for k in net.core.rmem_max net.core.wmem_max net.core.rmem_default net.core.wmem_default \
         net.ipv4.udp_mem net.ipv4.udp_rmem_min net.ipv4.udp_wmem_min \
         net.core.netdev_max_backlog net.core.netdev_budget net.core.netdev_budget_usecs \
         net.core.optmem_max net.core.somaxconn net.core.default_qdisc net.ipv4.tcp_congestion_control; do
    printf '%-28s %s\n' "$k" "$(sysctl -n "$k" 2>/dev/null || echo 'absent')"
done
p "fs.file-max" "$(sysctl -n fs.file-max)"
p "ulimit_nofile_soft" "$(ulimit -Sn)"
p "ulimit_nofile_hard" "$(ulimit -Hn)"

echo "== clock =="
p "timedatectl" "$(timedatectl 2>/dev/null | sed -n 's/^ *\(System clock synchronized\|NTP service\|Time zone\): */\1= /p' | tr '\n' ';')"
p "ntp_tools" "$(command -v chronyc ntpdig ntpq sntp 2>/dev/null | tr '\n' ' ' || true)"
p "hv_timesync" "$(lsmod 2>/dev/null | grep -c hv_utils) hv_utils module(s)"

echo "== listeners (relay) =="
ss -lunp 2>/dev/null | grep -E ':(9668|9768)\b' || echo "no UDP listener on 9668/9768"
ss -ltnp 2>/dev/null | grep -E ':(9668|9768)\b' || echo "no TCP listener on 9668/9768"
