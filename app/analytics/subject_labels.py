# app/analytics/subject_labels.py
"""Машинный идентификатор → человеческое имя (спека §6).

fail-open by design: неизвестный subject печатается сырым. Уведомление
обязано дойти даже с некрасивым именем — молчание хуже жаргона.

Список сверен с прод-БД 2026-08-04 (15 детекторов в detector_state) — НЕ
наполнять по памяти: первая версия писалась так и содержала 4 несуществующих
id, двое из которых отбирали номер у реальных (D44/D45). Актуальный список:
SELECT detector FROM detector_state ORDER BY 1.
"""

from __future__ import annotations

_DETECTOR_LABELS = {
    "D3_cross_mkt_arb": "D3 «Расхождение цен между рынками»",
    "D13_retail_confidence_fade": "D13 «Против розничной уверенности»",
    "D14_crypto_winners_distribute_no": "D14 «Крипто-победители»",
    "D26_fade_whale_extreme": "D26 «Против крайностей китов»",
    "D29_negrisk_basket_arb": "D29 «Арбитраж negrisk-корзины»",
    "D36_obi_pressure": "D36 «Давление стакана»",
    "D38_resolution_lag": "D38 «Задержка резолва»",
    "D40_kalshi_cross_platform": "D40 «Кросс-платформа с Kalshi»",
    "D42_event_driven_ml": "D42 «Событийная ML-модель»",
    "D44_late_convergence_carry": "D44 «Поздняя конвергенция»",
    "D45b_lead_lag_cross_venue": "D45b «Лаг между площадками»",
    "d45_chainlink_leadlag": "D45 «Лаг за Chainlink»",
    "D49_strike_ladder": "D49 «Лестница страйков»",
    "M2_liquidity_vacuum": "M2 «Вакуум ликвидности»",
    "STANDALONE_V2_LGB": "ML-модель V2 (LightGBM)",
}


# Инфраструктурные subject'ы — не детекторы, но попадают владельцу в тот же
# бриф. Без словаря рендер печатал бы сырое "infra:db_size", то есть жаргон
# ровно того сорта, от которого уводит спека §1 п.2.
_INFRA_LABELS = {
    "infra:db_size": "База данных",
    "infra:proxy": "Прокси для ордеров",
    "infra:wallet": "Кошелёк",
    "infra:d45_stream": "Поток котировок D45",
    "infra:detector_emission": "Поток сигналов детекторов",
    "infra:data_quality": "Качество данных",
    "analytics:adverse_selection": "Связки, где мы проигрываем систематически",
    "analytics:demote": "Детекторы-кандидаты на понижение",
    "analytics:virtual_promote": "Детекторы на бумажной торговле",
    "analytics:honest_audit": "Честный аудит по факту сделок",
}


def humanize_subject(subject: str) -> str:
    """'detector:D38_resolution_lag' → 'D38 «Задержка резолва»'.

    Неизвестные id возвращаются как есть (fail-open, см. docstring модуля).
    """
    if subject in _INFRA_LABELS:
        return _INFRA_LABELS[subject]
    if not subject.startswith("detector:"):
        return subject
    raw = subject.removeprefix("detector:").split(":category:")[0]
    return _DETECTOR_LABELS.get(raw, raw)
