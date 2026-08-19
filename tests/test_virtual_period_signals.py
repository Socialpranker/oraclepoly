"""Tests для fetch_virtual_period_signals SQL shape."""


def test_fetch_virtual_period_signals_returns_same_keys_as_shadow():
    """fetch_virtual_period_signals должна возвращать те же ключи что fetch_shadow_signals_7d.

    Это гарантирует что bot.py может заменить shadow_map без изменений caller-кода.
    """
    from app.services.detector_metrics import (
        _SHADOW_SIGNALS_7D_SQL,
        _VIRTUAL_PERIOD_SIGNALS_SQL,
    )
    shadow_aliases = set()
    for line in _SHADOW_SIGNALS_7D_SQL.splitlines():
        if " AS " in line:
            alias = line.split(" AS ")[-1].strip().rstrip(",").rstrip()
            shadow_aliases.add(alias)

    virtual_aliases = set()
    for line in _VIRTUAL_PERIOD_SIGNALS_SQL.splitlines():
        if " AS " in line:
            alias = line.split(" AS ")[-1].strip().rstrip(",").rstrip()
            virtual_aliases.add(alias)

    assert shadow_aliases == virtual_aliases, (
        f"Несовпадающие алиасы.\n"
        f"Только в shadow: {shadow_aliases - virtual_aliases}\n"
        f"Только в virtual: {virtual_aliases - shadow_aliases}"
    )
