#!/usr/bin/env python
import os
import sys
import argparse


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line
    from django.core.management.base import handle_default_options
    from django.conf import settings
    from config.runtime import runtime_lock

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--settings")
    parser.add_argument("--pythonpath")
    options, _ = parser.parse_known_args(sys.argv[1:])
    handle_default_options(options)
    if len(sys.argv) > 1 and sys.argv[1] in {"backup_local", "restore_local"}:
        execute_from_command_line(sys.argv)
    else:
        with runtime_lock(settings.AIQA_DATA_DIR, config_hash=settings.AIQA_CONFIG_HASH):
            execute_from_command_line(sys.argv)
