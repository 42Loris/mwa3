#!/bin/sh

# install dependencies
apt-get update
apt-get install -y p7zip-full dmg2img libmagic1t64 || apt-get install -y p7zip-full dmg2img libmagic1

# migrate database
python manage.py makemigrations catalogs pkgsinfo manifests icons santa process munkiwebadmin
python manage.py migrate

python manage.py collectstatic --noinput

gunicorn --bind=0.0.0.0 --timeout 600 --workers=4 munkiwebadmin.wsgi
