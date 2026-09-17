from pathlib import Path

from config.runtime import load_runtime


BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME = load_runtime()
AIQA_DATA_DIR = RUNTIME["data_dir"]
AIQA_INSTANCE_ID = RUNTIME["instance_id"]
AIQA_CONFIG_HASH = RUNTIME["config_hash"]
AIQA_CODEX_BIN = RUNTIME["AIQA_CODEX_BIN"]
AIQA_DOCKER_IMAGE = RUNTIME["AIQA_DOCKER_IMAGE"]
AIQA_ACCESS_MODE = RUNTIME["AIQA_ACCESS_MODE"]
AIQA_BIND = RUNTIME["AIQA_BIND"]
AIQA_PUBLIC_ORIGIN = RUNTIME["AIQA_PUBLIC_ORIGIN"]
SECRET_KEY = RUNTIME["secret_key"]
DEBUG = RUNTIME["AIQA_DEBUG"]
ALLOWED_HOSTS = [RUNTIME["host"]]
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.staticfiles",
    "apps.accounts", "apps.projects", "apps.conversations", "apps.work_requests",
    "apps.runs", "apps.ai", "apps.artifacts", "apps.approvals", "apps.operations",
]
MIDDLEWARE = [
    "config.middleware.LocalAccessMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "apps.artifacts.uploads.AttachmentUploadLimitMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.middleware.AccountSessionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "config.middleware.DomainErrorMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
AUTH_USER_MODEL = "accounts.User"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates", Path(__file__).parent / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"libraries": {"navigation": "apps.navigation"}, "context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3", "NAME": AIQA_DATA_DIR / "db.sqlite3",
    "OPTIONS": {"timeout": 5, "transaction_mode": "IMMEDIATE", "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;"},
}}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 6}},
]
LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "projects:list"
LOGOUT_REDIRECT_URL = "accounts:login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 43200
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_SECURE = AIQA_ACCESS_MODE == "lan_https"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_TRUSTED_ORIGINS = [AIQA_PUBLIC_ORIGIN]
SECURE_SSL_REDIRECT = AIQA_ACCESS_MODE == "lan_https"
SECURE_HSTS_SECONDS = 3600 if SESSION_COOKIE_SECURE else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
STATIC_URL = "/static/"
STATIC_ROOT = AIQA_DATA_DIR / "static"
STATICFILES_DIRS = [p for p in [BASE_DIR / "static", Path(__file__).parent / "static"] if p.is_dir()]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MEDIA_ROOT = AIQA_DATA_DIR / "uploads"
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 16 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 3
DEFAULT_CHARSET = "utf-8"


from django.db.backends.signals import connection_created


def protect_database(sender, connection, **kwargs):
    name = str(connection.settings_dict["NAME"])
    if connection.vendor == "sqlite" and name and not name.startswith(("file:", ":memory:")):
        file = Path(name)
        if file.exists():
            file.chmod(0o600)


connection_created.connect(protect_database, dispatch_uid="aiqa-private-db")
