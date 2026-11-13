###########
# BUILDER #
###########
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN sed -i 's/^Components: .*/& contrib non-free/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       gcc build-essential pkg-config python3-dev \
       default-libmysqlclient-dev libpq-dev \
       libxml2-dev libxslt1-dev zlib1g-dev \
       libsasl2-dev libldap2-dev libssl-dev libffi-dev \
       libjpeg-dev liblcms2-dev libblas-dev libopenblas-dev \
       curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

COPY requirements_docker.txt .
RUN pip install --upgrade pip \
    && pip wheel --no-cache-dir --no-deps --wheel-dir /usr/src/app/wheels -r requirements_docker.txt

# Munki-Tools fix auf 6.7.1: ab Munki 7 fehlt das Python-makecatalogs in code/client
ARG MUNKI_VERSION=6.7.1
RUN curl -fsSL -o /tmp/munki.tar.gz \
       "https://github.com/munki/munki/archive/refs/tags/v${MUNKI_VERSION}.tar.gz" \
    && mkdir -p /tmp/munkitools \
    && tar -xzf /tmp/munki.tar.gz -C /tmp/munkitools --strip-components=1 \
    && test -f /tmp/munkitools/code/client/makecatalogs \
    && rm /tmp/munki.tar.gz

#########
# FINAL #
#########
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app \
    APP_HOME=/home/app/web

# App-User mit FESTER UID/GID 1000 (muss zu securityContext im Helm-Chart passen)
RUN groupadd -g 1000 app \
    && useradd -u 1000 -g app -m -d /home/app -s /usr/sbin/nologin app \
    && mkdir -p $APP_HOME/staticfiles /munkirepo /munkitools /data

# Laufzeit-Pakete (cifs-utils entfernt: Mount im Container geht ohne privileged nicht)
RUN sed -i 's/^Components: .*/& contrib non-free/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       nginx libpq5 libmagic1t64 \
       fuse3 p7zip-full dmg2img libfsapfs-utils sleuthkit hfsprogs bzip2 \
       smbclient cifs-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/src/app/wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

COPY --from=builder /tmp/munkitools/code/client /munkitools

WORKDIR $APP_HOME
COPY . $APP_HOME
COPY munkiwebadmin/munkiwebadmin.conf /etc/nginx/sites-available/default

RUN chmod +x $APP_HOME/entrypoint.prod.sh \
    && python manage.py collectstatic --noinput \
    && sed -i 's|/run/nginx.pid|/tmp/nginx.pid|g' /etc/nginx/nginx.conf \
    && sed -i 's|^user www-data;|# user www-data;|' /etc/nginx/nginx.conf \
    && chown -R app:app /home/app /munkirepo /munkitools /data \
                        /var/lib/nginx /var/log/nginx /etc/nginx

#SER 1000:1000

VOLUME ["/munkirepo", "/data"]
EXPOSE 8080

ENTRYPOINT ["/home/app/web/entrypoint.prod.sh"]
