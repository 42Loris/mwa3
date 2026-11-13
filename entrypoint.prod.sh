#!/bin/sh
# MWA3 Entrypoint – angepasst für Kubernetes
#  - bricht bei Fehlern ab (statt still weiterzulaufen)
#  - gunicorn-Log geht auf stdout → sichtbar in kubectl/Argo CD Logs
#  - kein Warten mit nc (nc ist im Image nicht installiert)
set -e
# ==========================================
# 1. ROOT CONTEXT: Mount SMB share
# ==========================================
if [ "$(id -u)" = "0" ]; then
  echo "Container started as root. Checking for SMB variables..."
  
  if [ -n "$SMB_SERVER" ] && [ -n "$SMB_SHARE" ] && [ -n "$SMB_USER" ] && [ -n "$SMB_PASS" ]; then
    echo "Mounting SMB share //$SMB_SERVER/$SMB_SHARE to /munkirepo..."
    
    # Der Mount ist in einem IF-Block, damit 'set -e' den Container bei einem Mount-Fehler 
    # nicht ohne Log-Ausgabe sofort killt.
    if mount -t cifs "//$SMB_SERVER/$SMB_SHARE" /munkirepo -o username="$SMB_USER",password="$SMB_PASS",uid=1000,gid=1000,dir_mode=0755,file_mode=0644; then
      echo "SMB Mount successful."
    else
      echo "CRITICAL: SMB Mount failed! Check K8s securityContext (needs privileged: true)."
      exit 1
    fi
  else
    echo "Skipping SMB mount: Missing one or more SMB environment variables."
  fi

  # Privilegien abgeben und das exakt selbe Skript als 'app' User neu ausführen
  echo "Dropping privileges to user 'app' (uid=1000)..."
  exec su -s /bin/sh app -c "$0 $@"
fi

# ==========================================
# 2. APP USER CONTEXT: Run Application
# ==========================================
# Alles ab hier läuft strikt als unprivilegierter User 'app' (UID 1000)
NAME="MunkiWebAdmin"
SOCKFILE=/home/app/gunicorn.sock
NUM_WORKERS="${GUNICORN_WORKERS:-3}"
DJANGO_SETTINGS_MODULE=munkiwebadmin.settings

cd /home/app/web

echo "Starting $NAME as $(id -un 2>/dev/null || id -u) (uid=$(id -u))"
echo "DB-Backend: ${DB:-sqlite} | Repo: ${MUNKI_REPO_URL:-file:///munkirepo}"

# SQLite-Verzeichnis anlegen (falls SQL_DATABASE gesetzt ist)
if [ -n "$SQL_DATABASE" ]; then
  mkdir -p "$(dirname "$SQL_DATABASE")"
fi

# Datenbank migrieren
python manage.py makemigrations catalogs process pkgsinfo manifests icons santa munkiwebadmin
python manage.py migrate --noinput

# Optional: Admin-User beim ersten Start anlegen
# (DJANGO_SUPERUSER_USERNAME / _EMAIL / _PASSWORD als Env setzen)
if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
  python manage.py createsuperuser --noinput || echo "Superuser existiert bereits"
fi

# gunicorn im Hintergrund, Log auf stdout
gunicorn munkiwebadmin.wsgi:application \
  --name "$NAME" \
  --workers "$NUM_WORKERS" \
  --bind "unix:$SOCKFILE" \
  --timeout 600 \
  --log-level info \
  --access-logfile - \
  --error-logfile - &

# warten bis der Socket existiert (max. 60 s)
i=0
while [ ! -S "$SOCKFILE" ]; do
  i=$((i+1))
  if [ "$i" -gt 60 ]; then
    echo "FEHLER: gunicorn ist nicht gestartet" >&2
    exit 1
  fi
  sleep 1
done
echo "gunicorn läuft, starte nginx"

exec nginx -g "daemon off;"
