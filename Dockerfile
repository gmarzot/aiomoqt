FROM python:3.12-slim

WORKDIR /app

# setuptools_scm needs a version when .git isn't present in the build
# context (which .dockerignore deliberately excludes to keep the image
# small). The release workflow passes the tag as --build-arg VERSION=x.y.z.
ARG VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}

COPY . .

RUN pip install --no-cache-dir .

# Role switch for the moq-interop-runner. The runner expects two
# container shapes from a single image: a client (default) and a
# relay (selected via MOQT_ROLE=relay). The relay binds UDP/4443 by
# default and loads /certs/cert.pem + /certs/priv.key per the runner's
# certs convention.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
