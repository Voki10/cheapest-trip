"""Both languages: parser notes, search texts, place names, plurals."""

from datetime import date

from cheaptrip.geo import get_geo
from cheaptrip.i18n import _T, nights, stops, tr
from cheaptrip.models import TripQuery
from cheaptrip.nlp import parse
from test_search import D, FakeFlights, FakeHotels, run, ticket


def test_every_text_has_both_languages():
    assert all(ru and en for ru, en in _T.values())
    assert tr("no_limit", "en") == "no limit" and tr("no_limit", "ru") == "без ограничений"
    assert tr("no_limit", "de") == "без ограничений"  # unknown language falls back to Russian


def test_plurals():
    assert [nights(n, "ru") for n in (1, 2, 5, 11, 21, 22)] == \
        ["1 ночь", "2 ночи", "5 ночей", "11 ночей", "21 ночь", "22 ночи"]
    assert [nights(n, "en") for n in (1, 2)] == ["1 night", "2 nights"]
    assert [stops(n, "ru") for n in (0, 1, 3, 5)] == ["без пересадок", "1 пересадка", "3 пересадки", "5 пересадок"]
    assert stops(1, "en") == "1 stop" and stops(4, "en") == "4 stops"


def test_parser_answers_in_the_chosen_language():
    q, notes = parse("из Москвы в Тбилиси на 5 ночей", get_geo(), today=date(2026, 10, 1), lang="en")
    assert (q.origin, q.destination, q.lang) == ("Moscow", "Tbilisi", "en")
    assert notes[0] == "From: city “Moscow”"
    q, notes = parse("из Москвы куда угодно", get_geo(), today=date(2026, 10, 1), lang="ru")
    assert (q.origin, q.destination) == ("Москва", "Куда угодно") and notes[0] == "Откуда: город «Москва»"


def test_search_answers_in_the_chosen_language():
    tickets = [ticket("MOW", "IST", D(11, 10), 5000), ticket("IST", "MOW", D(11, 15), 5000, hour=4)]
    q = TripQuery(origin="Moscow", destination="Istanbul", nights_min=5, nights_max=5, lang="en")
    rep = run(q, FakeFlights(tickets), FakeHotels(None))
    r = rep.results[0]
    assert (r.destination, r.origin) == ("Istanbul", "Moscow")
    assert rep.interpreted["tripLength"] == "5 nights" and rep.interpreted["flightBudget"] == "no limit"
    assert any(w.startswith("Early-morning return flight") for w in r.warnings)
    rep = run(q.model_copy(update={"lang": "ru"}), FakeFlights(tickets), FakeHotels(None))
    assert rep.results[0].destination == "Стамбул" and rep.interpreted["tripLength"] == "5 ночей"


def test_english_examples_from_the_page():
    g, today = get_geo(), date(2026, 10, 1)
    q = parse("From Moscow to anywhere for a weekend", g, today=today, lang="en")[0]
    assert (q.origin, q.nights_min, q.nights_max) == ("Moscow", 2, 2)  # "weekend" is not "a week"
    q = parse("Somewhere warm in January for a week, up to 80000 rub with hotel", g, today=today, lang="en")[0]
    assert q.total_budget == 80000 and q.hotel_budget_per_night is None and q.nights_min == 7 and q.weather == "warm"
    q = parse("From Saint Petersburg to Asia for at least 2 weeks, hotel up to $40", g, today=today, lang="en")[0]
    assert (q.destination, q.nights_min, q.hotel_budget_per_night) == ("Asia", 14, 40)
