# MoQ Ersatz Relay

`aiomoqt-relay.marzresearch.net` runs the aiomoqt ersatz relay for limited interop testing, its
dashboard, and the G5 Broadcast and G5 Player test pages. The relay is open, with no auth.

| Service | Container | What it does |
|---|---|---|
| `relay` | `ersatz-relay` | `moq_interop_relay` on UDP 4443, raw QUIC and WebTransport, drafts 22/20/18/16/14, `--watchdog 15`, writes `status/` and `relay-logs/` |
| `web` | `relay-web` | nginx on 80/443: landing page, `/g5-broadcast/`, `/g5-player/`, `/relay/` dashboard, `/relay/platform/` and `/relay/logs/`, `/status/{status,platform}.json`, `/private/`, `/stats/` |
| `goaccess` | `relay-goaccess` | rebuilds the visitor reports in `stats/` every 5 minutes from `logs/`; no network |

Host: `ssh ubuntu@aiomoqt-relay.marzresearch.net` (129.146.35.142; OCI, Ubuntu 24.04 aarch64,
Docker with Compose v2). The subnet's security list opens UDP 4433-4443 and TCP 80/443; the host's
own iptables (`/etc/iptables/rules.v4`) must also accept TCP 443 and UDP 4443. The certificate
comes from apt certbot with dns-cloudflare (`/etc/cloudflare/credentials.ini`, 0600). Its deploy
hooks in `/etc/letsencrypt/renewal-hooks/deploy/` restart `ersatz-relay` and reload `relay-web` by
name.

The host is also the perf and release-gate box. UDP 4443 belongs to the public relay; test relays
use 5443 and up. Before a bench that needs a quiet box, `docker stop ersatz-relay`, and start it
again after.

## Layout on the host

Everything lives in `~/aiomoqt-relay`; run every `docker compose` command from there.

```
compose.yml, nginx.conf   this directory
site/index.html           landing page (site/ here)
site/relay/, site/private/
                          dashboard, platform, log viewer and private index (site/ here)
site/assets, site/g5-broadcast, site/g5-player, site/VERSION
                          moq-playa examples/dist
status/                   status.json and platform.json, written by the relay, read-only to nginx
relay-logs/               relay.log (10 MB, 4 rotations) and crash.log, written by the relay,
                          read-only to nginx
cores/                    relay core files
logs/                     nginx access log, read by GoAccess
stats/                    GoAccess reports: index.html (visitors), crawlers.html
stats.htpasswd            the password for /private/, /stats/ and /relay/logs/
build/                    relay image build context
```

## First-time setup

Create `stats.htpasswd` before the first `up`. If it is missing, Docker creates a directory in its
place and `/stats/` fails.

```sh
tar -C deploy/aiomoqt-relay -cf - compose.yml nginx.conf site | ssh ubuntu@aiomoqt-relay.marzresearch.net 'mkdir -p ~/aiomoqt-relay && tar -C ~/aiomoqt-relay -xf -'
ssh -t ubuntu@aiomoqt-relay.marzresearch.net
cd ~/aiomoqt-relay && mkdir -p status relay-logs cores logs stats
printf 'moqadmin:%s\n' "$(openssl passwd -6)" > stats.htpasswd    # prompts for the password
sudo tee /etc/logrotate.d/aiomoqt-relay >/dev/null <<'ROT'
/home/ubuntu/aiomoqt-relay/logs/access.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
ROT
docker compose up -d
```

Certificate and firewall, once:

```sh
sudo apt-get install -y certbot python3-certbot-dns-cloudflare
sudo certbot certonly --dns-cloudflare --dns-cloudflare-credentials /etc/cloudflare/credentials.ini -d aiomoqt-relay.marzresearch.net
sudo iptables -I INPUT 5 -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT 5 -p udp --dport 4443 -j ACCEPT
sudo netfilter-persistent save
```

## Host tuning

The relay's picoquic worker thread is the bottleneck under fan-out, mostly per-packet kernel work, so
the host keeps other per-packet work off its cores:

- **No connection tracking for the relay's port**, in the iptables `raw` table. The `INPUT` rule for
  UDP 4443 still admits the untracked packets. `/etc/iptables/rules.v4` holds curated Docker chains,
  so add the two rules to its `*raw` table by hand; `netfilter-persistent save` would capture
  transient container rules.

  ```sh
  sudo iptables -t raw -I PREROUTING -p udp --dport 4443 -j NOTRACK
  sudo iptables -t raw -I OUTPUT -p udp --sport 4443 -j NOTRACK
  sudo sh -c 'iptables-restore --test < /etc/iptables/rules.v4'    # after editing the file
  ```

- **NIC interrupts on CPU 0, the relay on CPUs 1-2.** `host/relay-irq-affinity.sh` pins the NIC's
  queue interrupts to CPU 0, and `host/relay-irq-affinity.service` runs it at boot (irqbalance is
  off). The relay service has `cpuset: "1,2"`.

  ```sh
  sudo install -m 755 host/relay-irq-affinity.sh /usr/local/sbin/
  sudo install -m 644 host/relay-irq-affinity.service /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now relay-irq-affinity.service
  ```

## Relay image

The relay runs the newest aiomoqt (`gmarzot-0.12.0`) and aiopquic (`gmarzot-0.5.0`) code, built on
the host from the repo's `Dockerfile`. The build context has no `.git`, so pass the version
explicitly. Run `python -m setuptools_scm` in the checkout to get it.

```sh
tar --exclude=.git --exclude=certs -cf - . | ssh ubuntu@aiomoqt-relay.marzresearch.net 'rm -rf ~/aiomoqt-relay/build && mkdir -p ~/aiomoqt-relay/build && tar -C ~/aiomoqt-relay/build -xf -'
ssh ubuntu@aiomoqt-relay.marzresearch.net 'docker build --build-arg VERSION=<pep440> -t aiomoqt:<sha> ~/aiomoqt-relay/build'
```

To run an aiopquic build that is not on PyPI, layer its wheel on top. The cp312 aarch64 wheel comes
from aiopquic's `wheels-dry-run.yml` artifacts (`gh run download -R gmarzot/aiopquic`). A
`pip install git+…` of aiopquic cannot build, because its picoquic is prebuilt by `build.sh`.

```dockerfile
FROM aiomoqt:<sha>
COPY aiopquic-*.whl /tmp/
RUN pip install --no-cache-dir --no-deps --force-reinstall /tmp/aiopquic-*.whl && rm /tmp/aiopquic-*.whl
```

Tag the result `aiomoqt:dev`, then `docker compose up -d relay`. To roll back, re-tag an earlier
image (`docker image ls aiomoqt`) and run `up -d relay` again.

## G5 pages

The pages are built from the fork gmarzot/moq-playa, branch `gmarzot-playa-dev`, which is forked from
openmoq/moq-playa:

```sh
pnpm -r build      # in the moq-playa checkout
tar -C examples/dist -cf - assets g5-broadcast g5-player | ssh ubuntu@aiomoqt-relay.marzresearch.net 'tar -C ~/aiomoqt-relay/site -xf -'
```

## nginx config

`nginx.conf` is bind-mounted as a single file. Replacing it gives it a new inode, and
`nginx -s reload` still reads the old one. After copying it, run `docker compose restart web`.

## Relay logs

`/private/` links the visitor reports and the relay logs. `/relay/logs/` tails the relay's
`--log-dir` behind the same password. `relay.log` rotates itself at 10 MB and keeps four old
files. `crash.log` gets a `relay: start` line at each start and the faulthandler dump of a native
crash or watchdog stall. It is not rotated, so trim it by hand if it grows.

## Core dumps

The relay runs with no core size limit and `./cores` mounted at `/cores`. Ubuntu pipes cores to
apport, which drops container crashes, so point the kernel at the mount (runtime only; reboot or an
apport restart resets it):

```sh
sudo sysctl -w kernel.core_pattern=/cores/core.%e.%p.%t
sudo install -d -m 1777 /cores    # cores of processes outside containers
```

The path resolves inside the crashing process's mount namespace. Cores are not rotated; keep the
image that produced one (`docker image ls aiomoqt`) for `gdb`, and delete them by hand.

## Checks, run on the host

```sh
curl -s https://aiomoqt-relay.marzresearch.net/status/status.json | python3 -c 'import json,sys,time; s=json.load(sys.stdin); print(round(time.time()-s["generated"]), "s old,", s["totals"]["sessions"], "sessions")'
docker run --rm --network host --entrypoint python aiomoqt:dev -c 'import asyncio; from aiomoqt.tools import moq_interop_relay as r; print(asyncio.run(r._self_probe("127.0.0.1", 4443, True, 18, 8.0)))'
curl -s -o /dev/null -w '%{http_code}\n' https://aiomoqt-relay.marzresearch.net/stats/    # 401 without the password
curl -s -o /dev/null -w '%{http_code}\n' https://aiomoqt-relay.marzresearch.net/relay/logs/files/relay.log    # 401 too
```

`docker logs ersatz-relay` shows a watchdog probe under `aiomoqt-watchdog/…` every 15 s. That is
expected, and the dashboard leaves it out.

## Taking it down

`docker compose down` stops everything; `docker stop ersatz-relay` stops just the relay.
