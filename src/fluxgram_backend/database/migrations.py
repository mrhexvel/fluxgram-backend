from importlib.resources import files

from alembic import command
from alembic.config import Config

from fluxgram_backend.settings import Settings


def run_migrations(settings: Settings) -> None:
    script_location = files("fluxgram_backend.database").joinpath("alembic")
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", settings.require_database_url().replace("%", "%%"))
    command.upgrade(config, "head")
