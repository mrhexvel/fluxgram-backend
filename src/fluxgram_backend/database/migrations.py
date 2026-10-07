from importlib.resources import files

from alembic import command
from alembic.config import Config

from fluxgram_backend.settings import Settings


def build_migration_config(settings: Settings) -> Config:
    script_location = files("fluxgram_backend.database").joinpath("alembic")
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", settings.require_database_url().replace("%", "%%"))
    return config


def run_migrations(settings: Settings) -> None:
    config = build_migration_config(settings)
    command.upgrade(config, "head")
