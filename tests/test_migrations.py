from importlib.resources import files


def test_plugin_reputation_hardening_validates_legacy_rows() -> None:
    migration = files("fluxgram_backend.database").joinpath(
        "alembic",
        "versions",
        "20261101_0009_harden_plugin_reputation_publication.py",
    )

    source = migration.read_text(encoding="utf-8")

    assert "LOCK TABLE public.plugin_reputation_records" in source
    assert "existing plugin reputation operation is invalid" in source
    assert "operation.result IS DISTINCT FROM jsonb_build_object" in source
    assert "FOR UPDATE" in source


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
    assert migration_root.joinpath(
        "versions",
        "20261010_0004_seed_fluxgram_user_1.py",
    ).is_file()
    assert migration_root.joinpath(
        "versions",
        "20261015_0005_seed_love_mans.py",
    ).is_file()
    assert migration_root.joinpath(
        "versions",
        "20261015_0006_update_love_mans_emoji.py",
    ).is_file()
    assert migration_root.joinpath(
        "versions",
        "20261016_0007_seed_donate.py",
    ).is_file()
    assert migration_root.joinpath(
        "versions",
        "20261101_0008_plugin_reputation_records.py",
    ).is_file()
    assert migration_root.joinpath(
        "versions",
        "20261101_0009_harden_plugin_reputation_publication.py",
    ).is_file()
