"""
WSGI config for pyrunner project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pyrunner.settings")

application = get_wsgi_application()

# help.search's docs index, built once per web process at boot rather than on the
# first question (2.5 T19). It never fails the boot.
from core.services.help_docs import warm  # noqa: E402

warm()
