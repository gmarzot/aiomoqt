# moqarena

`moqarena.marzresearch.net` serves the G5 Broadcast and G5 Player test pages, and runs the aiomoqt
ersatz relay for limited interop testing. The relay is open, with no auth.

| Service | Container | What it does |
|---|---|---|
| `relay` | `ersatz-relay` | `moq_interop_relay` on UDP 4443, raw QUIC and WebTransport, drafts 22/20/18/16/14, `--watchdog 15`, writes `status/status.json` and `relay-logs/` |
| `web` | `g5-web` | nginx on 80/443: landing page, `/g5-broadcast/`, `/g5-player/`, `/relay/` dashboard, `/relay/platform/` and `/relay/logs/`, `/status/{status,platform}.json`, `/stats/` |
| `goaccess` | `g5-goaccess` | rebuilds the visitor reports in `stats/` every 5 minutes from `logs/`; no network |

Host: `ssh ubuntu@moqarena.marzresearch.net` (OCI, Ubuntu 22.04 aarch64, Docker with Compose v2).
The OCI security list opens UDP 4433-4443 and TCP 80/443. The certificate comes from snap certbot
with dns-cloudflare. Its deploy hooks in `/etc/letsencrypt/renewal-hooks/deploy/` restart
`ersatz-relay` and reload `g5-web` by name.

## Layout on the host

Everything lives in `~/g5-site`; run every `docker compose` command from there.

```
compose.yml, nginx.conf   this directory
site/index.html           landing page (site/ here)
site/relay/               relay dashboard (site/ here)
site/assets, site/g5-broadcast, site/g5-player, site/VERSION
                          moq-playa examples/dist
status/                   status.json and platform.json, written by the relay, read-only to nginx
relay-logs/               relay.log (10 MB, 4 rotations) and crash.log, written by the relay,
                          read-only to nginx
logs/                     nginx access log, read by GoAccess
stats/                    GoAccess reports: index.html (visitors), crawlers.html
stats.htpasswd            the password for /stats/ and /relay/logs/
```

## First-time setup

Create `stats.htpasswd` before the first `up`. If it is missing, Docker creates a directory in its
place and `/stats/` fails.

```sh
tar -C deploy/moqarena -cf - compose.yml nginx.conf site | ssh ubuntu@moqarena.marzresearch.net 'tar -C ~/g5-site -xf -'
ssh -t ubuntu@moqarena.marzresearch.net
cd ~/g5-site && mkdir -p status relay-logs logs stats
printf 'gmarzot:%s\n' "$(openssl passwd -apr1)" > stats.htpasswd    # prompts for the password
sudo tee /etc/logrotate.d/moqarena >/dev/null <<'ROT'
/home/ubuntu/g5-site/logs/access.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
ROT
docker rm -f ersatz-relay      # a relay started by hand; compose recreates it
docker compose up -d
```

## Relay image

The image is built on the host from the repo's `Dockerfile`. The build context has no `.git`, so
pass the version explicitly. Run `python -m setuptools_scm` in the checkout to get it.

```sh
tar --exclude=.git --exclude=certs -cf - . | ssh ubuntu@moqarena.marzresearch.net 'rm -rf ~/Projects/aiomoqt && mkdir -p ~/Projects/aiomoqt && tar -C ~/Projects/aiomoqt -xf -'
ssh ubuntu@moqarena.marzresearch.net 'docker build --build-arg VERSION=<pep440> -t aiomoqt:<sha> ~/Projects/aiomoqt'
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
tar -C examples/dist -cf - assets g5-broadcast g5-player | ssh ubuntu@moqarena.marzresearch.net 'tar -C ~/g5-site/site -xf -'
```

## nginx config

`nginx.conf` is bind-mounted as a single file. Replacing it gives it a new inode, and
`nginx -s reload` still reads the old one. After copying it, run `docker compose restart web`.

## Relay logs

`/relay/logs/` tails the relay's `--log-dir` behind the `/stats/` password; full peer addresses
appear only there. `relay.log` rotates itself at 10 MB and keeps four old files. `crash.log` gets
a `relay: start` line at each start and the faulthandler dump of a native crash or watchdog stall.
It is not rotated, so trim it by hand if it grows. `--log-dir` needs an aiomoqt relay that has the
flag; on an older image, drop it from `command`, or the relay restarts in a loop.

## Checks, run on the host

```sh
curl -s https://moqarena.marzresearch.net/status/status.json | python3 -c 'import json,sys,time; s=json.load(sys.stdin); print(round(time.time()-s["generated"]), "s old,", s["totals"]["sessions"], "sessions")'
docker run --rm --network host --entrypoint python aiomoqt:dev -c 'import asyncio; from aiomoqt.tools import moq_interop_relay as r; print(asyncio.run(r._self_probe("127.0.0.1", 4443, True, 18, 8.0)))'
curl -s -o /dev/null -w '%{http_code}\n' https://moqarena.marzresearch.net/stats/    # 401 without the password
curl -s -o /dev/null -w '%{http_code}\n' https://moqarena.marzresearch.net/relay/logs/files/relay.log    # 401 too
```

`docker logs ersatz-relay` shows a watchdog probe under `aiomoqt-watchdog/…` every 15 s. That is
expected, and the dashboard leaves it out.

## Taking it down

`docker compose down` stops everything; `docker stop ersatz-relay` stops just the relay.
