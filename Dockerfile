FROM python:3.12-slim

WORKDIR /app

# setuptools_scm needs a version when .git isn't present in the build
# context (which .dockerignore deliberately excludes to keep the image
# small). The release workflow passes the tag as --build-arg VERSION=x.y.z.
ARG VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}

COPY . .

RUN pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "aiomoqt.examples.moq_interop_client"]
