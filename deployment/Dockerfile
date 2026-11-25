ARG BASE_IMAGE=turb-gpt-free-register:20261126-roxy
FROM ${BASE_IMAGE}

USER root
COPY requirements.txt /tmp/camoufox-requirements.txt
RUN pip install --no-cache-dir -r /tmp/camoufox-requirements.txt pytest \
    && pip check \
    && python -m playwright install-deps firefox

# The existing /home/turb bind mount must not hide the baked browser cache.
ENV XDG_CACHE_HOME=/opt/browser-cache
RUN python -m camoufox fetch \
    && chown -R turb:turb /opt/browser-cache \
    && chmod -R a+rX /opt/browser-cache

ARG GIT_REVISION
LABEL org.opencontainers.image.revision=${GIT_REVISION}
ENV TURB_GIT_REVISION=${GIT_REVISION}
COPY --chown=turb:turb . /opt/turb-source
USER turb
WORKDIR /app
