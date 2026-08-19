#!/usr/bin/env python3
"""Forward-probe: реальная книга CLOB в момент METAR lock-in на temperature-рынках.

Зачем: ретроспективно вопрос «торгуем ли lock-in gap» НЕразрешим — наши
price-снапшоты пишут markets.current_price (15-мин market_sync), и «реальный
gap живёт ~10 мин» неотличим от «stale-фид поверх мгновенного sweep»
(memory temperature-lockin-edge-measured-2026-07-07). Этот скрипт собирает
forward-данные, которые различают гипотезы: живую книгу CLOB в момент лока
и её распад по секундам.

Запуск: ТОЛЬКО на проде (VPS), на хосте (нужен docker exec к postgres):
    nohup python3 scripts/research/temp_lockin_forward_probe.py \
        >> /root/oracle/research/output/temp_lockin_probe/probe.log 2>&1 &
Остановка: kill по pid из лога. Горизонт сбора: 5-7 дней, потом анализ JSONL.

Самопроверка без сети/БД: python3 ... --self-test

Что пишет (JSONL, research/output/temp_lockin_probe/events-YYYYMMDD.jsonl):
  {"type":"metar", ...}  — новое METAR-наблюдение станции (только при изменении)
  {"type":"lock",  ...}  — переход рынка OPEN → NO_LOCKED/YES_LOCKED
  {"type":"book",  ...}  — снимок книги (top-5 bid/ask) обоих токенов рынка:
                           каждый цикл для всех рынков дня (baseline-серия)
                           + внеочередные через +30/+60/+120/+300с после лока.

Lock-логика идентична валидированной 178/178 (analyze.py ресёрча 2026-07-07).
Rate limits: METAR — 1 GET/цикл на все станции; CLOB /books — POST батчами
по 50 токенов, 2-6 запросов/цикл при цикле 75с (лимит 500/10с — запас ×100).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

POLL_SEC = 75
MARKETS_REFRESH_SEC = 600
BOOK_TOP_N = 5
POST_LOCK_SHOTS_SEC = (30, 60, 120, 300)
OUT_DIR = Path("/root/oracle/research/output/temp_lockin_probe")
STATE_PATH = OUT_DIR / "state.json"

METAR_URL = "https://aviationweather.gov/api/data/metar?ids={ids}&format=json&hours=3"
BOOKS_URL = "https://clob.polymarket.com/books"

PSQL_CMD = [
    "docker",
    "exec",
    "-i",
    "oraclepoly-postgres",
    "psql",
    "-U",
    "oraclepoly",
    "-d",
    "oraclepoly",
    "-t",
    "-A",
    "-F",
    "\t",
]

MARKETS_SQL = """
SELECT polymarket_id, question, clob_token_ids->>0, clob_token_ids->>1, end_date
  FROM markets
 WHERE question ILIKE '%temperature%'
   AND is_active = true
   AND end_date BETWEEN now() - interval '12 hours' AND now() + interval '48 hours'
"""

# Город → ICAO. Источник: wunderground-ссылки в description рынков
# (ресёрч 2026-07-07, 46 станций). Hong Kong резолвится по HKO (не METAR) —
# берём VHHH как приближение и помечаем в записи station_proxy=true.
CITY_ICAO = {
    "amsterdam": "EHAM",
    "ankara": "LTAC",
    "atlanta": "KATL",
    "austin": "KAUS",
    "beijing": "ZBAA",
    "buenos aires": "SAEZ",
    "busan": "RKPK",
    "cape town": "FACT",
    "chengdu": "ZUUU",
    "chicago": "KORD",
    "chongqing": "ZUCK",
    "dallas": "KDAL",
    "denver": "KBKF",
    "guangzhou": "ZGGG",
    "helsinki": "EFHK",
    "hong kong": "VHHH",
    "houston": "KHOU",
    "istanbul": "LTFM",
    "jeddah": "OEJN",
    "karachi": "OPKC",
    "kuala lumpur": "WMKK",
    "london": "EGLC",
    "los angeles": "KLAX",
    "lucknow": "VILK",
    "madrid": "LEMD",
    "manila": "RPLL",
    "mexico city": "MMMX",
    "miami": "KMIA",
    "milan": "LIMC",
    "moscow": "UUEE",
    "munich": "EDDM",
    "new york city": "KLGA",
    "panama city": "MPMG",
    "paris": "LFPB",
    "qingdao": "ZSQD",
    "san francisco": "KSFO",
    "sao paulo": "SBGR",
    "seattle": "KSEA",
    "seoul": "RKSI",
    "shanghai": "ZSPD",
    "shenzhen": "ZGSZ",
    "singapore": "WSSS",
    "taipei": "RCSS",
    "tel aviv": "LLBG",
    "tokyo": "RJTT",
    "toronto": "CYYZ",
    "warsaw": "EPWA",
    "wellington": "NZWN",
    "wuhan": "ZHHH",
}
STATION_PROXY = {"VHHH"}  # резолв не по METAR этой станции, данные приближение

ICAO_TZ = {
    "CYYZ": "America/Toronto",
    "EDDM": "Europe/Berlin",
    "EFHK": "Europe/Helsinki",
    "EGLC": "Europe/London",
    "EHAM": "Europe/Amsterdam",
    "EPWA": "Europe/Warsaw",
    "FACT": "Africa/Johannesburg",
    "KATL": "America/New_York",
    "KAUS": "America/Chicago",
    "KBKF": "America/Denver",
    "KDAL": "America/Chicago",
    "KHOU": "America/Chicago",
    "KLAX": "America/Los_Angeles",
    "KLGA": "America/New_York",
    "KMIA": "America/New_York",
    "KORD": "America/Chicago",
    "KSEA": "America/Los_Angeles",
    "KSFO": "America/Los_Angeles",
    "LEMD": "Europe/Madrid",
    "LFPB": "Europe/Paris",
    "LIMC": "Europe/Rome",
    "LLBG": "Asia/Jerusalem",
    "LTAC": "Europe/Istanbul",
    "LTFM": "Europe/Istanbul",
    "MMMX": "America/Mexico_City",
    "MPMG": "America/Panama",
    "NZWN": "Pacific/Auckland",
    "OEJN": "Asia/Riyadh",
    "OPKC": "Asia/Karachi",
    "RCSS": "Asia/Taipei",
    "RJTT": "Asia/Tokyo",
    "RKPK": "Asia/Seoul",
    "RKSI": "Asia/Seoul",
    "RPLL": "Asia/Manila",
    "SAEZ": "America/Argentina/Buenos_Aires",
    "SBGR": "America/Sao_Paulo",
    "UUEE": "Europe/Moscow",
    "VHHH": "Asia/Hong_Kong",
    "VILK": "Asia/Kolkata",
    "WMKK": "Asia/Kuala_Lumpur",
    "WSSS": "Asia/Singapore",
    "ZBAA": "Asia/Shanghai",
    "ZGGG": "Asia/Shanghai",
    "ZGSZ": "Asia/Shanghai",
    "ZHHH": "Asia/Shanghai",
    "ZSPD": "Asia/Shanghai",
    "ZSQD": "Asia/Shanghai",
    "ZUCK": "Asia/Shanghai",
    "ZUUU": "Asia/Shanghai",
}

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_MONTHS_ALT = "|".join(m.capitalize() for m in MONTHS)

QUESTION_RE = re.compile(
    r"Will the (highest|lowest) temperature in ([A-Za-z .]+?) be "
    r"(?:between (-?\d+)-(-?\d+)°([CF])"
    r"|(-?\d+)°([CF]) or below"
    r"|(-?\d+)°([CF]) or higher"
    r"|(-?\d+)°([CF]))"
    rf" on ({_MONTHS_ALT}) (\d+)\??$"
)


def parse_question(q: str, year: int) -> dict | None:
    """(direction, city, kind, lo, hi, unit, month, day) из текста вопроса.

    Формулировки стабильны (проверено на всех 683 рынках ресёрча); новые
    шаблоны просто не матчатся и рынок пропускается (пишем в лог).
    """
    m = QUESTION_RE.match(q.strip())
    if not m:
        return None
    if m.group(3) is not None:
        kind, lo, hi, unit = "range", float(m.group(3)), float(m.group(4)), m.group(5)
    elif m.group(6) is not None:
        kind, lo, hi, unit = "or_below", None, float(m.group(6)), m.group(7)
    elif m.group(8) is not None:
        kind, lo, hi, unit = "or_higher", float(m.group(8)), None, m.group(9)
    else:
        kind, lo, hi, unit = "exact", float(m.group(10)), float(m.group(10)), m.group(11)
    return {
        "direction": m.group(1),
        "city": m.group(2).lower(),
        "kind": kind,
        "lo": lo,
        "hi": hi,
        "unit": unit,
        "month": MONTHS[m.group(12).lower()],
        "day": int(m.group(13)),
        "year": year,
    }


def outcome_state(direction: str, kind: str, lo: float | None, hi: float | None, running: float) -> str:
    """'YES_LOCKED' | 'NO_LOCKED' | 'OPEN' — детерминирован ли исход running-экстремумом.

    Идентично analyze.py ресёрча 2026-07-07 (валидировано 178/178 против резолвов).
    """
    x = running
    if direction == "highest":  # running max, может только расти
        if kind in ("exact", "range", "or_below") and hi is not None and x > hi:
            return "NO_LOCKED"
        if kind == "or_higher" and lo is not None and x >= lo:
            return "YES_LOCKED"
    else:  # lowest — running min, может только падать
        if kind in ("exact", "range", "or_higher") and lo is not None and x < lo:
            return "NO_LOCKED"
        if kind == "or_below" and hi is not None and x <= hi:
            return "YES_LOCKED"
    return "OPEN"


def f_from_c(c: float) -> float:
    return c * 9.0 / 5.0 + 32


# --- I/O ---------------------------------------------------------------------


def http_json(url: str, payload: list | None = None, timeout: int = 30):
    # CLOB (Cloudflare) отдаёт 403 на дефолтный "Python-urllib/3.x" UA —
    # проверено с прод-IP 2026-07-07: curl-UA 200, python-UA 403.
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; oraclepoly-probe/1.0)"})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_markets() -> list[dict]:
    out = subprocess.run(PSQL_CMD + ["-c", MARKETS_SQL], capture_output=True, text=True, timeout=60, check=True)
    markets = []
    for line in out.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        pid, question, tok_yes, tok_no, end_date = parts
        parsed = parse_question(question, year=datetime.now(UTC).year)
        if not parsed:
            log("unparsed_question", question=question[:80])
            continue
        icao = CITY_ICAO.get(parsed["city"])
        if not icao or icao not in ICAO_TZ:
            log("unknown_city", city=parsed["city"])
            continue
        markets.append(
            {
                "pid": pid,
                "question": question,
                "tok_yes": tok_yes,
                "tok_no": tok_no,
                "icao": icao,
                **parsed,
            }
        )
    return markets


def fetch_metar_batch(icaos: list[str]) -> dict[str, list[tuple[int, float]]]:
    """icao → [(obs_epoch, temp_c)], отсортировано по времени."""
    url = METAR_URL.format(ids=",".join(sorted(set(icaos))))
    rows = http_json(url)
    by_station: dict[str, list[tuple[int, float]]] = {}
    for r in rows:
        temp = r.get("temp")
        if temp is None:
            continue
        by_station.setdefault(r["icaoId"], []).append((int(r["obsTime"]), float(temp)))
    for v in by_station.values():
        v.sort()
    return by_station


def fetch_books(token_ids: list[str]) -> dict[str, dict]:
    """token_id → {'bids': top-N, 'asks': top-N} (лучшие первыми)."""
    books: dict[str, dict] = {}
    for i in range(0, len(token_ids), 50):
        chunk = token_ids[i : i + 50]
        rows = http_json(BOOKS_URL, payload=[{"token_id": t} for t in chunk])
        for b in rows:
            # CLOB отдаёт уровни от худшего к лучшему — лучшие в конце списка
            bids = sorted(b.get("bids", []), key=lambda x: -float(x["price"]))
            asks = sorted(b.get("asks", []), key=lambda x: float(x["price"]))
            books[b["asset_id"]] = {
                "bids": [[lv["price"], lv["size"]] for lv in bids[:BOOK_TOP_N]],
                "asks": [[lv["price"], lv["size"]] for lv in asks[:BOOK_TOP_N]],
            }
    return books


_out_file = None


def emit(record: dict) -> None:
    global _out_file
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"events-{datetime.now(UTC):%Y%m%d}.jsonl"
    record["ts"] = datetime.now(UTC).isoformat(timespec="milliseconds")
    if _out_file is None or Path(_out_file.name) != path:
        if _out_file:
            _out_file.close()
        _out_file = open(path, "a")
    _out_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    _out_file.flush()


def log(msg: str, **kw) -> None:
    print(f"{datetime.now(UTC):%H:%M:%S} {msg} {kw if kw else ''}", flush=True)


# --- main loop ---------------------------------------------------------------


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"locks": {}}  # pid → lock-info (переживает рестарт, лок пишем один раз)


def save_state(state: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def running_extreme(
    series: list[tuple[int, float]], direction: str, day_start: datetime, day_end: datetime
) -> tuple[float, int] | None:
    """(extreme, epoch последнего вклада) по наблюдениям внутри local-суток."""
    lo_e, hi_e = int(day_start.timestamp()), int(day_end.timestamp())
    vals = [(ts, v) for ts, v in series if lo_e <= ts < hi_e]
    if not vals:
        return None
    pick = max if direction == "highest" else min
    best = pick(vals, key=lambda p: p[1])
    return best[1], best[0]


def main() -> None:
    state = load_state()
    markets: list[dict] = []
    markets_at = 0.0
    last_obs: dict[str, int] = {}  # icao → последний увиденный obs_epoch
    pending_shots: list[tuple[float, dict, str]] = []  # (due, market, label)

    log("probe_started", poll_sec=POLL_SEC)
    while True:
        cycle_start = time.time()
        try:
            if cycle_start - markets_at > MARKETS_REFRESH_SEC:
                markets = fetch_markets()
                markets_at = cycle_start
                log("markets_refreshed", n=len(markets))

            icaos = [m["icao"] for m in markets]
            metar = fetch_metar_batch(icaos) if icaos else {}

            for icao, series in metar.items():
                if series and series[-1][0] != last_obs.get(icao):
                    last_obs[icao] = series[-1][0]
                    emit({"type": "metar", "icao": icao, "obs_epoch": series[-1][0], "temp_c": series[-1][1]})

            # lock-детекция per рынок
            for m in markets:
                if m["pid"] in state["locks"]:
                    continue
                tz = ZoneInfo(ICAO_TZ[m["icao"]])
                day_start = datetime(m["year"], m["month"], m["day"], tzinfo=tz)
                day_end = day_start + timedelta(days=1)
                ext = running_extreme(metar.get(m["icao"], []), m["direction"], day_start, day_end)
                if ext is None:
                    continue
                temp_c, obs_epoch = ext
                temp = f_from_c(temp_c) if m["unit"] == "F" else temp_c
                st = outcome_state(m["direction"], m["kind"], m["lo"], m["hi"], temp)
                if st == "OPEN":
                    continue
                lock = {
                    "state": st,
                    "obs_epoch": obs_epoch,
                    "running": temp,
                    "detected_at": datetime.now(UTC).isoformat(),
                    "station_proxy": m["icao"] in STATION_PROXY,
                }
                state["locks"][m["pid"]] = lock
                save_state(state)
                emit({"type": "lock", "pid": m["pid"], "question": m["question"], "icao": m["icao"], **lock})
                log("LOCK", q=m["question"][:60], state=st)
                now = time.time()
                for dt_s in POST_LOCK_SHOTS_SEC:
                    pending_shots.append((now + dt_s, m, f"post_lock_{dt_s}s"))

            # книги: baseline для всех рынков, чьи сутки уже начались, + внеочередные
            due = [(mm, lbl) for t, mm, lbl in pending_shots if t <= time.time()]
            pending_shots = [p for p in pending_shots if p[0] > time.time()]
            now_utc = datetime.now(UTC)
            baseline = [
                m
                for m in markets
                if datetime(m["year"], m["month"], m["day"], tzinfo=ZoneInfo(ICAO_TZ[m["icao"]])) <= now_utc
            ]
            shot_list = [(m, "baseline") for m in baseline] + due
            tokens = sorted(
                {m["tok_yes"] for m, _ in shot_list if m["tok_yes"]}
                | {m["tok_no"] for m, _ in shot_list if m["tok_no"]}
            )
            books = fetch_books(tokens) if tokens else {}
            for m, label in shot_list:
                emit(
                    {
                        "type": "book",
                        "pid": m["pid"],
                        "label": label,
                        "lock_state": state["locks"].get(m["pid"], {}).get("state", "OPEN"),
                        "yes": books.get(m["tok_yes"]),
                        "no": books.get(m["tok_no"]),
                    }
                )
        except Exception as e:  # probe не должен умирать от единичного сбоя
            log("cycle_error", error=f"{type(e).__name__}: {e}")

        # внеочередные снимки точнее по времени, чем цикл: спим до ближайшего due
        next_due = min((t for t, _, _ in pending_shots), default=cycle_start + POLL_SEC)
        time.sleep(max(1.0, min(cycle_start + POLL_SEC, next_due) - time.time()))


# --- self-test ----------------------------------------------------------------


def self_test() -> None:
    q = "Will the highest temperature in Amsterdam be 22°C on July 9?"
    p = parse_question(q, 2026)
    assert p and p["kind"] == "exact" and p["lo"] == p["hi"] == 22.0, p
    assert CITY_ICAO[p["city"]] == "EHAM"
    assert outcome_state("highest", "exact", 22.0, 22.0, 23.0) == "NO_LOCKED"
    assert outcome_state("highest", "exact", 22.0, 22.0, 22.0) == "OPEN"
    assert outcome_state("highest", "or_higher", 30.0, None, 30.0) == "YES_LOCKED"
    assert outcome_state("lowest", "or_below", None, 20.0, 19.5) == "YES_LOCKED"
    assert outcome_state("lowest", "range", 60.0, 61.0, 59.0) == "NO_LOCKED"
    p2 = parse_question("Will the lowest temperature in Miami be between 78-79°F on July 3?", 2026)
    assert p2 and p2["direction"] == "lowest" and p2["unit"] == "F" and p2["hi"] == 79.0, p2
    for city in CITY_ICAO.values():
        assert city in ICAO_TZ, f"нет таймзоны для {city}"
    print("self-test OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        sys.exit(0)
    main()
