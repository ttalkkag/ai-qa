import os
import tempfile


os.environ["AIQA_DATA_DIR"] = tempfile.mkdtemp(prefix="aiqa-tests-")
os.environ["AIQA_ACCESS_MODE"] = "loopback"
os.environ["AIQA_PUBLIC_ORIGIN"] = "http://127.0.0.1:8000"
os.environ["AIQA_BIND"] = "127.0.0.1:8000"
os.environ["AIQA_TRUST_PROXY"] = "false"
from config.settings import *


ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
STATIC_ROOT.mkdir(mode=0o700, exist_ok=True)
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
