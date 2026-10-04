"""Natural-language request → TripQuery (Russian + basic English), rule based and deterministic.

Every assumption is returned as a note so the user sees exactly how the request was understood.
When the user asks for "the cheapest" and names no price, budgets stay None (= No limit).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .config import settings
from .geo import Geo, Place
from .i18n import nights_range, norm_lang, tr
from .models import TripQuery
from .search import add_months, fmt_money

MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_RE = (r"(январ[ьяе]|феврал[ьяе]|март[ае]?|апрел[ьяе]|ма[йяе]|июн[ьяе]|июл[ьяе]|август[ае]?|"
            r"сентябр[ьяе]|октябр[ьяе]|ноябр[ьяе]|декабр[ьяе]|"
            r"january|february|march|april|may|june|july|august|september|october|november|december)")
MONTH_NAMES_RU = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
                  "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTH_NAMES_EN = ["", "January", "February", "March", "April", "May", "June", "July", "August",
                  "September", "October", "November", "December"]

# Words that follow "в/на/до" but are never places ("в течение", "на неделю", "до 30 тысяч").
STOPWORDS = {
    "течение", "пределах", "районе", "среднем", "сумме", "целом", "итоге", "любое", "любой", "любую",
    "неделю", "недели", "неделе", "выходные", "выходных", "море", "моря", "пляж", "отпуск", "каникулы",
    "тепло", "теплое", "теплые", "теплую", "теплый", "теплом", "жаркое", "жаркую", "страну", "город",
    "месяц", "месяца", "месяцев", "дней", "день", "дня", "ночь", "ночи", "ночей", "сутки", "год", "году",
    "одну", "одной", "две", "два", "три", "пару", "несколько", "человека", "человек", "двоих", "одного",
    "самый", "самое", "самую", "дешево", "дешевле", "максимум", "минимум", "время", "дату", "даты",
    "следующем", "следующей", "этой", "этом", "ближайшие", "ближайший", "ближайшее", "новый", "центре",
    "центра", "отель", "отеле", "отелем", "рублей", "долларов", "евро", "тысяч", "тыс", "завтраком",
    "сторону", "обе", "оба", "конце", "начале", "середине", "качестве", "поездку", "путешествие",
    "долларах", "рублях", "валюте", "январе", "феврале", "марте", "апреле", "мае", "июне", "июле",
    "августе", "сентябре", "октябре", "ноябре", "декабре", "anytime", "january", "the", "a",
    "понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье", "отеле", "хостеле",
    "самолете", "поезде", "машине", "праздники", "отпуске", "командировку",
}
# Generic words that may stand between a preposition and the name: «на остров Пхукет», «в город Сочи».
FILLERS = {"остров", "острова", "полуостров", "город", "города", "курорт", "курорты", "страну", "страны",
           "штат", "область", "регион", "провинцию", "побережье", "столицу"}
STOPWORDS -= FILLERS

ANYWHERE_RE = r"куда[- ]?(?:нибудь|угодно|то)|куда глаза глядят|в любую страну|anywhere|somewhere"

NUM = r"\d{1,3}(?:[  ]\d{3})+|\d+(?:[.,]\d+)?"
MONEY_RE = re.compile(
    rf"(?P<pre>[$€₽])?\s*(?P<num>{NUM})\s*"
    rf"(?P<mult>тысяч[аи]?|тыс\.?|к\b|k\b)?\s*"
    rf"(?P<cur>руб\w*|р\b\.?|₽|rub\b|долл\w*|бакс\w*|\$|usd\b|евро|eur\b|€)?",
    re.IGNORECASE,
)
BUDGET_CUE_RE = re.compile(r"(до|максимум|макс\.?|не дороже|не более|бюджет\w*|за|под|в пределах|under|max)\s*$")

TOTAL_KW = re.compile(r"вместе с|с отел|с жиль|всего|итого|в сумме|общ\w*|на вс[её]|полност|под ключ|"
                      r"вс[её] включ|total|in total|all in|with (?:the )?hotel|including (?:the )?hotel|incl\.? hotel")
HOTEL_KW = re.compile(r"отел|гостиниц|жиль|прожив|хостел|апартамент|ночь|ночлег|hotel|night")
FLIGHT_KW = re.compile(r"перел[её]т|билет|авиа|самол[её]т|flight|ticket|air")

CUR_MAP = [(re.compile(r"^(руб|р\b|р\.|₽|rub)", re.I), "RUB"),
           (re.compile(r"^(долл|бакс|\$|usd)", re.I), "USD"),
           (re.compile(r"^(евро|eur|€)", re.I), "EUR")]


@dataclass
class Money:
    amount: float
    currency: str | None
    start: int
    end: int


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def _currency(token: str | None) -> str | None:
    if not token:
        return None
    for rx, code in CUR_MAP:
        if rx.match(token):
            return code
    return None


def _find_money(t: str) -> list[Money]:
    out = []
    for m in MONEY_RE.finditer(t):
        raw = m.group("num")
        after = t[m.end("num"):m.end("num") + 12]
        # Skip durations, counts and dates: "5 дней", "3 месяцев", "2 пересадки", "15 ноября".
        if re.match(rf"\s*(дн|ноч|сут|недел|месяц|мес\b|год|лет|пересад|звезд|км|km|чел|взросл|stops?|night|day|week|month|"
                    rf"{MONTH_RE}|[-–]\s*\d+\s*(дн|ноч))", after):
            continue
        if re.search(r"(рейтинг\w*|rating)\s*$", t[:m.start()]):
            continue
        num = float(raw.replace(" ", "").replace(" ", "").replace(",", "."))
        mult = (m.group("mult") or "").lower()
        if mult:
            num *= 1000
        currency = _currency(m.group("pre")) or _currency(m.group("cur"))
        cue = BUDGET_CUE_RE.search(t[max(0, m.start() - 20):m.start()])
        if not (currency or mult or (cue and num >= 10)):
            continue
        out.append(Money(num, currency, m.start(), m.end()))
    return out


def _clause(t: str, start: int, end: int) -> tuple[str, str]:
    left = t[:start]
    cut = max(left.rfind(","), left.rfind(";"), left.rfind(" и "), left.rfind(" а "), left.rfind("."))
    before = left[cut + 1:] if cut >= 0 else left
    right = t[end:]
    stop = min([i for i in (right.find(","), right.find(";"), right.find(" и "), right.find(".")) if i >= 0]
               or [len(right)])
    return before, right[:stop]


def _classify(t: str, m: Money) -> str:
    before, after = _clause(t, m.start, m.end)
    if re.match(r"\s*(за|/|в|per|a)\s*(ночь|сутки|night)", after) or re.match(r"\s*/\s*ноч", after):
        return "hotel"
    ctx = before + " " + after
    if TOTAL_KW.search(ctx):
        return "total"
    if HOTEL_KW.search(before) or HOTEL_KW.search(after):
        return "hotel"
    if FLIGHT_KW.search(ctx):
        return "flight"
    return "total"


ORIGIN_PREPS = {"из", "from", "с", "со"}
DEST_PREPS = {"в", "во", "на", "до", "to", "по"}
LIST_SEPS = {",", ";", "/", "или", "и", "либо", "or", "and"}
EXCLUDE_TRIGGERS = {"кроме", "исключая", "except", "excluding", "без", "не", "not"}
ANYWHERE_DEST_TOKENS = {"куда-нибудь", "куда-то", "anywhere", "везде", "somewhere"}
ANYWHERE_ORIGIN_RE = re.compile(r"откуда[- ]?(?:угодно|нибудь|то)\b|из любо(?:й|го) (?:страны|города|места|точки|аэропорта)|"
                                r"from anywhere")


def _tokens(t: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9][a-zа-я0-9\-]*|[,;/]", t)


def _place_after(geo: Geo, words: list[str], i: int, fuzzy: bool = True,
                 typos: list | None = None) -> tuple[Place, int] | None:
    """Longest place name starting at token i (1–4 words, never across a separator).
    fuzzy: also match states by Russian stem ("в Калифорнию") — only right after a preposition."""
    if i >= len(words):
        return None
    if words[i] in FILLERS:
        hit = _place_after(geo, words, i + 1, fuzzy, typos)
        return (hit[0], hit[1] + 1) if hit else None
    first = words[i]
    if first in STOPWORDS or first in LIST_SEPS or first[0].isdigit():
        return None
    for k in range(min(4, len(words) - i), 0, -1):
        window = words[i:i + k]
        if any(w in (",", ";", "/") for w in window):
            continue
        place = geo.lookup(" ".join(window), fuzzy=fuzzy)
        if place is None:
            continue
        if place.kind == "city" and geo.cities[place.code].routes == 0 and k == 1 and len(first) < 5:
            continue  # tiny airfield whose name is a short common word
        return place, k
    if typos is not None:  # last resort, right after a preposition only: «на Пхует» → Пхукет
        for k in range(min(2, len(words) - i), 0, -1):
            window = words[i:i + k]
            if any(w in (",", ";", "/") or w in STOPWORDS or w in LIST_SEPS for w in window):
                continue
            hit = geo.lookup_typo(" ".join(window))
            if hit:
                typos.append((" ".join(window), hit[0]))
                return hit[0], k
    return None


def _place_list(geo: Geo, words: list[str], i: int, skip: set[str],
                typos: list | None = None) -> tuple[list[Place], int]:
    """"Москвы, Питера или Казани" → three places. `skip` = tokens allowed before each next item
    ("или из Казани", "и не в Гомель"); a preposition of the other role ends the list."""
    hit = _place_after(geo, words, i, typos=typos)
    if hit is None:
        return [], i
    places, k = [hit[0]], i + hit[1]
    while k < len(words):
        m = k
        while m < len(words) and words[m] in LIST_SEPS:
            m += 1
        if m == k:
            break
        while m < len(words) and words[m] in skip:
            m += 1
        nxt = _place_after(geo, words, m, typos=typos)
        if nxt is None:
            break
        places.append(nxt[0])
        k = m + nxt[1]
    return places, k


def _form_value(geo: Geo, place: Place, lang: str = "ru") -> str:
    """Human-readable form text that resolves back to exactly this place (else its code)."""
    if place.kind == "multi":
        return ", ".join(_form_value(geo, m, lang) for m in place.members)
    value = place.form_value(lang)
    back = geo.resolve_one(value)
    if back is not None and (back.kind, back.code) == (place.kind, place.code):
        return value
    return place.code or value


def _parse_places(geo: Geo, t: str):
    words = _tokens(t)
    origin: list[Place] = []
    dest: list[Place] = []
    excl = {"origin": [], "dest": []}
    typos: list[tuple[str, Place]] = []
    consumed: set[int] = set()
    last_role = None
    origin_anywhere = ANYWHERE_ORIGIN_RE.search(t) is not None
    i = 0
    while i < len(words):
        w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else ""
        # "куда угодно / куда-нибудь" and "откуда угодно" set the role that a later "кроме" refers to.
        if w in ANYWHERE_DEST_TOKENS or (w == "куда" and nxt in ("угодно", "нибудь")):
            last_role = "dest"
        elif w.startswith("откуда") or (w == "из" and nxt.startswith("любо")):
            last_role = "origin"
        # Exclusions: "кроме Минска", "но не в Минск", "не из Москвы", "за исключением Турции".
        trigger = w in EXCLUDE_TRIGGERS or (w == "за" and nxt == "исключением")
        if trigger:
            j = i + (2 if w == "за" else 1)
            prep = words[j] if j < len(words) and words[j] in ORIGIN_PREPS | DEST_PREPS else None
            if prep:
                j += 1
            places, end = _place_list(geo, words, j, {"не", *(ORIGIN_PREPS if prep in ORIGIN_PREPS else DEST_PREPS)},
                                      typos)
            if places:
                role = ("origin" if prep in ORIGIN_PREPS else "dest") if prep else (last_role or "dest")
                excl[role].extend(places)
                consumed.update(range(i, end))
                i = end
                continue
        if w in ORIGIN_PREPS or w == "из-за":
            places, end = _place_list(geo, words, i if w == "из-за" else i + 1, ORIGIN_PREPS, typos)
            if places and not origin:
                origin, last_role = places, "origin"
                consumed.update(range(i, end))
                i = end
                continue
        if w in DEST_PREPS:
            places, end = _place_list(geo, words, i + 1, DEST_PREPS, typos)
            if places and not dest:
                dest, last_role = places, "dest"
                consumed.update(range(i, end))
                i = end
                continue
        i += 1
    if not origin and not dest and not origin_anywhere:
        # "Москва — Стамбул", "Moscow Istanbul": bare names in order.
        found, i = [], 0
        while i < len(words):
            hit = None if i in consumed else _place_after(geo, words, i, fuzzy=False)
            if hit:
                found.append(hit[0])
                i += hit[1]
            else:
                i += 1
        if len(found) >= 2:
            origin, dest = [found[0]], [found[1]]
        elif len(found) == 1:
            dest = [found[0]]
    return origin, dest, excl, origin_anywhere, typos


def _month_window(month: int, today: date) -> tuple[date, date]:
    year = today.year if month >= today.month else today.year + 1
    start = date(year, month, 1)
    end = add_months(start, 1) - timedelta(days=1)
    return start, end


def _month_num(token: str) -> int | None:
    token = token.lower()
    for stem, n in sorted(MONTHS.items(), key=lambda x: -len(x[0])):
        if token.startswith(stem):
            return n
    return None


def parse(text: str, geo: Geo, today: date | None = None, default_origin: str | None = None,
          lang: str = "ru") -> tuple[TripQuery, list[str]]:
    lang = norm_lang(lang)
    default_origin = default_origin or ("Russia" if lang == "en" else "Россия")
    today = today or date.today()

    def t_(key: str, **kw) -> str:
        return tr(key, lang, **kw)

    q: dict = {"lang": lang}

    t = _norm(text)
    notes: list[str] = []

    # ── places ───────────────────────────────────────────────────────────────
    origin_list, dest_list, excl, origin_anywhere, typos = _parse_places(geo, t)
    for wrong, place in typos:
        notes.append(t_("nl_typo", wrong=wrong, right=place.name(lang)))
    # A capitalised word after a preposition that is not a known place: say so instead of silently
    # searching "anywhere" ("в Нарнию" → "не узнал место «Нарнию»").
    for m in re.finditer(r"(?<![\w-])(?:в|во|на|из|до|кроме|без)\s+([А-ЯЁA-Z][\w\-]*(?:\s+[\w\-]+){0,2})", text):
        words = m.group(1).split()
        if not any(geo.lookup(" ".join(words[:k]), fuzzy=True) or geo.lookup_typo(" ".join(words[:k]))
                   for k in range(1, min(len(words), 2) + 1)):
            notes.append(t_("nl_unknown", word=words[0]))
    origin = geo.combine(origin_list)
    dest = geo.combine(dest_list)
    anywhere = re.search(ANYWHERE_RE, t) is not None
    if origin is not None:
        q["origin"] = _form_value(geo, origin, lang)
        notes.append(t_("nl_from", place=origin.describe(lang)))
    elif origin_anywhere:
        q["origin"] = "Anywhere" if lang == "en" else "Откуда угодно"
        notes.append(t_("nl_from_anywhere"))
    else:
        q["origin"] = default_origin
        notes.append(t_("nl_from_default", place=default_origin))
    if dest is not None:
        q["destination"] = _form_value(geo, dest, lang)
        notes.append(t_("nl_to", place=dest.describe(lang)))
    else:
        q["destination"] = "Anywhere" if lang == "en" else "Куда угодно"
        notes.append(t_("nl_to_anywhere") if anywhere else t_("nl_to_unspecified"))
    if excl["origin"]:
        q["exclude_origins"] = [_form_value(geo, p, lang) for p in excl["origin"]]
        notes.append(t_("nl_excl_from", places=", ".join(p.describe(lang) for p in excl["origin"])))
    if excl["dest"]:
        q["exclude_destinations"] = [_form_value(geo, p, lang) for p in excl["dest"]]
        notes.append(t_("nl_excl_to", places=", ".join(p.describe(lang) for p in excl["dest"])))

    # ── trip length ──────────────────────────────────────────────────────────
    lo, hi = settings.flexible_nights_min, settings.flexible_nights_max
    if m := re.search(r"(?:на|от)?\s*(\d+)\s*(?:[-–]|до)\s*(\d+)\s*(дн|ноч|сут|days?|nights?)", t):
        a, b = int(m.group(1)), int(m.group(2))
        q["nights_min"], q["nights_max"] = min(a, b), max(a, b)
    elif m := re.search(r"(?:от|не меньше|не менее|минимум|хотя бы|at least|min(?:imum)?)\s*"
                        r"(недели|месяца|(\d+|двух|трех|трёх)\s*недел\w*|a week|(\d+) weeks)", t):
        count = m.group(2) or m.group(3)
        weeks = {"двух": 2, "трех": 3, "трёх": 3}.get(count or "") or (int(count) if count else 1)
        q["nights_min"] = 30 if m.group(1) == "месяца" else 7 * weeks
        q["nights_max"] = hi
    elif m := re.search(r"(?:от|не меньше|не менее|минимум|хотя бы|at least|min(?:imum)?)\s*(\d+)\s*"
                        r"(дн|день|ноч|сут|days?|nights?)", t):
        q["nights_min"], q["nights_max"] = int(m.group(1)), hi
    elif m := re.search(r"(?:до|не больше|не более|максимум|up to|max(?:imum)?)\s*(\d+)\s*"
                        r"(дн|день|ноч|сут|days?|nights?)", t):
        q["nights_min"], q["nights_max"] = lo, int(m.group(1))
    elif m := re.search(r"(\d+)\s*(дн[яейь]|день|ноч\w*|сут\w*|days?|nights?)", t):
        q["nights_min"] = q["nights_max"] = int(m.group(1))
    elif m := re.search(r"(\d+|две|два|три|пару)\s*недел", t):
        n = {"две": 2, "два": 2, "три": 3, "пару": 2}.get(m.group(1)) or int(m.group(1))
        q["nights_min"] = q["nights_max"] = 7 * n
    elif re.search(r"на недел|\ba week\b|\bone week\b", t):
        q["nights_min"] = q["nights_max"] = 7
    elif re.search(r"на выходн|weekend", t):
        q["nights_min"] = q["nights_max"] = 2
    elif re.search(r"на пару дней|пару дней", t):
        q["nights_min"] = q["nights_max"] = 2
    elif re.search(r"несколько дней", t):
        q["nights_min"], q["nights_max"] = 3, 5
    elif re.search(r"на месяц", t):
        q["nights_min"] = q["nights_max"] = 30
    elif m := re.search(r"на (\d+|два|две|три|пару) месяц", t):
        n = {"два": 2, "две": 2, "три": 3, "пару": 2}.get(m.group(1)) or int(m.group(1))
        q["nights_min"] = q["nights_max"] = 30 * n
    if "nights_min" in q and (q["nights_min"] < lo or q["nights_max"] > hi):
        asked = (q["nights_min"], q["nights_max"])
        q["nights_min"] = min(max(q["nights_min"], lo), hi)
        q["nights_max"] = min(max(q["nights_max"], q["nights_min"]), hi)
        notes.append(t_("nl_nights_clamped", lo=lo, hi=hi,
                        asked=f"{asked[0]}" + (f"–{asked[1]}" if asked[1] != asked[0] else "")))

    # ── dates ────────────────────────────────────────────────────────────────
    if m := re.search(r"(?:ближайш\w*|следующ\w*|в течение|next)\s*(\d+|пол)\s*(?:-?х|-?ти)?\s*(месяц|мес|month|год|лет)", t):
        n = 6 if m.group(1) == "пол" else int(m.group(1))
        months = n * 12 if m.group(2).startswith(("год", "лет")) else n
        q["search_months"] = months
        notes.append(t_("nl_period_months", n=months))
    elif re.search(r"(ближайш\w*|в течение)\s*(месяц|month)", t):
        q["search_months"] = 1
        notes.append(t_("nl_period_month"))
    elif re.search(r"ближайш\w* полгода|в течение полугода", t):
        q["search_months"] = 6
        notes.append(t_("nl_period_months", n=6))
    elif re.search(r"(ближайш\w*|в течение) год", t):
        q["search_months"] = 12
        notes.append(t_("nl_period_months", n=12))
    elif m := re.search(rf"с\s*(\d{{1,2}})\s*(?:{MONTH_RE}\s*)?по\s*(\d{{1,2}})\s*{MONTH_RE}", t):
        d1, d2 = int(m.group(1)), int(m.group(3))
        month2 = _month_num(m.group(4))
        month1 = _month_num(m.group(2)) if m.group(2) else month2
        y = today.year if (month1, d1) >= (today.month, today.day) else today.year + 1
        try:
            start = date(y, month1, d1)
            end = date(y if month2 >= month1 else y + 1, month2, d2)
            q["date_from"] = q["date_to"] = start
            if "nights_min" not in q:
                q["nights_min"] = q["nights_max"] = (end - start).days
            notes.append(t_("nl_dates", start=f"{start:%d.%m.%Y}", end=f"{end:%d.%m.%Y}"))
        except ValueError:
            pass
    elif ms := re.findall(rf"(?:в|во|in)\s+{MONTH_RE}(?:\s*(?:[-–]|или|и|or)\s*{MONTH_RE})?", t):
        first = _month_num(ms[0][0])
        last = _month_num(ms[0][1]) if ms[0][1] else first
        start, _ = _month_window(first, today)
        _, end = _month_window(last, start if last >= first else today)
        if end < start:
            end = add_months(end.replace(day=1), 12 + 1) - timedelta(days=1)
        q["date_from"], q["date_to"] = start, end
        names = MONTH_NAMES_EN if lang == "en" else MONTH_NAMES_RU
        label = names[first] + (f"–{names[last]}" if last != first else "")
        notes.append(t_("nl_period_label", label=label, start=f"{start:%d.%m.%Y}", end=f"{end:%d.%m.%Y}"))
    elif re.search(r"в следующем месяце|next month", t):
        start = add_months(today.replace(day=1), 1)
        q["date_from"], q["date_to"] = start, add_months(start, 1) - timedelta(days=1)
        notes.append(t_("nl_next_month", month=f"{start:%m.%Y}"))
    elif re.search(r"на следующей неделе|next week", t):
        start = today + timedelta(days=7 - today.weekday())
        q["date_from"], q["date_to"] = start, start + timedelta(days=6)
        notes.append(t_("nl_next_week", start=f"{start:%d.%m}", end=f"{start + timedelta(days=6):%d.%m}"))
    else:
        notes.append(t_("nl_anytime"))
    if "nights_min" in q:
        n1, n2 = q["nights_min"], q["nights_max"]
        notes.append(t_("nl_nights", nights=nights_range(n1, n2, lang)))
    else:
        notes.append(t_("nl_flexible"))

    # ── money ────────────────────────────────────────────────────────────────
    for money in _find_money(t):
        kind = _classify(t, money)
        if kind == "hotel":
            cur = money.currency
            if cur is None:
                cur = "USD" if money.amount < 1000 else "RUB"
                notes.append(t_("nl_hotel_currency", cur=cur))
            q["hotel_budget_per_night"], q["hotel_budget_currency"] = money.amount, cur
            notes.append(t_("nl_hotel_budget", money=fmt_money(money.amount, cur, lang)))
        else:
            cur = money.currency or "RUB"
            q.setdefault("currency", cur)
            key = "flight_budget" if kind == "flight" else "total_budget"
            q[key] = money.amount
            notes.append(t_("nl_flight_budget" if kind == "flight" else "nl_total_budget",
                            money=fmt_money(money.amount, cur, lang)))
    if "flight_budget" not in q:
        notes.append(t_("nl_no_flight_budget"))
    if re.search(r"дешевл\w* всего|самы\w* дешев|подешевле|cheapest", t) and not any(
            k in q for k in ("flight_budget", "total_budget", "hotel_budget_per_night")):
        notes.append(t_("nl_cheapest"))
    if re.search(r"можно дороже|и дороже|выше бюджета|above budget", t):
        q["show_above_budget"] = True

    # ── other constraints ────────────────────────────────────────────────────
    if re.search(r"тепл|жарк|на море|пляж|солн|warm|beach|sunny", t):
        q["weather"] = "warm"
        notes.append(t_("nl_warm"))
    if re.search(r"без пересад|прям(ой|ым|ые|ого)|direct|non-?stop", t):
        q["max_stops"] = 0
        notes.append(t_("nl_direct"))
    elif m := re.search(r"(?:не более|максимум|до|не больше)\s*(\d|одн\w*|двух|трех)\s*пересад", t):
        n = {"одн": 1, "дву": 2, "тре": 3}.get(m.group(1)[:3]) if not m.group(1).isdigit() else int(m.group(1))
        q["max_stops"] = n
        notes.append(t_("nl_stops_max", n=n))
    if re.search(r"за границ|заграниц|за рубеж|abroad", t):
        q["include_domestic"] = False
    if re.search(r"по росси", t) and dest is None:
        q["destination"] = "RU"
        q["include_domestic"] = True
    if re.search(r"с завтрак|завтрак включ|breakfast", t):
        q["breakfast"] = True
        notes.append(t_("nl_breakfast"))
    if re.search(r"отдельн\w* (комнат|номер)|приватн|private room|без хостел|не хостел", t):
        q["private_room"] = True
        notes.append(t_("nl_private"))
    if m := re.search(r"(?:рейтинг\w*|rating)\s*(?:от|не ниже|>=?)?\s*(\d(?:[.,]\d)?)", t):
        q["min_hotel_rating"] = float(m.group(1).replace(",", "."))
    if m := re.search(r"(?:до|не дальше|в пределах)\s*(\d+(?:[.,]\d)?)\s*км\s*от центр", t):
        q["max_distance_km"] = float(m.group(1).replace(",", "."))
    elif re.search(r"в центре|city cent", t):
        q["max_distance_km"] = 2.0
        notes.append(t_("nl_centre"))
    if re.search(r"в долларах|in usd|in dollars", t):
        q["currency"] = "USD"
    elif re.search(r"в евро\b|in eur", t):
        q["currency"] = "EUR"

    return TripQuery(**q), notes
