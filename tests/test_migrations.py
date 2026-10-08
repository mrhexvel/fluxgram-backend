from importlib.resources import files


def test_migration_assets_are_packaged_with_database_module() -> None:
    migration_root = files("fluxgram_backend.database").joinpath("alembic")

    assert migration_root.joinpath("env.py").is_file()
    assert migration_root.joinpath("script.py.mako").is_file()
    assert migration_root.joinpath("versions").is_dir()
    assert migration_root.joinpath(
        "versions",
        "20261007_0001_appearance_schema.py",
    ).is_file()
    assert migration_root.joinpath(
        "versions",
        "20261007_0002_seed_badges.py",
    ).is_file()
    assert migration_root.joinpath(
        "versions",
        "20261008_0003_dynamic_badge_definitions.py",
    ).is_file()
