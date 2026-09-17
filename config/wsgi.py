import atexit
import os

from django.core.wsgi import get_wsgi_application
from django.conf import settings

from config.runtime import runtime_lock


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
_process_lock = runtime_lock(settings.AIQA_DATA_DIR, config_hash=settings.AIQA_CONFIG_HASH)
_process_lock.__enter__()
atexit.register(lambda: _process_lock.__exit__(None, None, None))
application = get_wsgi_application()
