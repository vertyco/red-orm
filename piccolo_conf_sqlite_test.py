import os

from piccolo.conf.apps import AppRegistry
from piccolo.engine.sqlite import SQLiteEngine

DB = SQLiteEngine(path=os.getenv("DB_PATH"))

APP_REGISTRY = AppRegistry(apps=["tests_sqlite.piccolo_app"])
