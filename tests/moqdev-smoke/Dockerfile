# moq-dev's relay smoke (moq-dev/moq test/smoke). The published moq clients need glibc >= 2.38.
FROM ubuntu:24.04
ARG PLAYWRIGHT_VERSION=1.64.1
# Playwright and vite need Node >= 20; Ubuntu 24.04 ships 18.
ARG NODE_VERSION=22.12.0
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip xz-utils git jq ffmpeg python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz | tar -xJ -C /usr/local --strip-components=1
ENV BUN_INSTALL=/opt/bun PATH=/opt/bun/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
# Runs as the host user: bun's package cache must be writable.
ENV BUN_INSTALL_CACHE_DIR=/tmp/bun-cache
# The harness skips its own browser download when PLAYWRIGHT_BROWSERS_PATH is set.
RUN curl -fsSL https://bun.sh/install | bash \
 && bunx playwright@${PLAYWRIGHT_VERSION} install --with-deps chromium \
 && chmod -R a+rX /opt/bun /opt/ms-playwright \
 && chmod -R a+rwX "$BUN_INSTALL_CACHE_DIR"
