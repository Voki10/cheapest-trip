from datetime import date

from cheaptrip.geo import get_geo
from cheaptrip.nlp import parse

TODAY = date(2026, 9, 29)


def p(text):
    return parse(text, get_geo(), today=TODAY)[0]


def test_spec_example_asia():
    q = p("Хочу из России куда-нибудь в Азию на 5 дней в течение ближайших 3 месяцев, "
          "перелёт максимум 25 000 рублей, отель максимум $40")
    assert q.origin == "Россия" and q.destination == "Азия"
    assert (q.nights_min, q.nights_max) == (5, 5) and q.search_months == 3
    assert q.flight_budget == 25000 and q.hotel_budget_per_night == 40 and q.hotel_budget_currency == "USD"


def test_spec_example_warm_january():
    q = p("Хочу куда-нибудь тёплое из Москвы на неделю в январе")
    assert q.origin == "Москва" and q.destination == "Куда угодно" and q.weather == "warm"
    assert (q.nights_min, q.nights_max) == (7, 7)
    assert q.date_from == date(2027, 1, 1) and q.date_to == date(2027, 1, 31)
    assert q.flight_budget is None


def test_spec_example_total_budget():
    q = p("Хочу в Азию до 30 тысяч вместе с отелем")
    assert q.destination == "Азия" and q.total_budget == 30000
    assert q.flight_budget is None and q.hotel_budget_per_night is None


def test_spec_example_cheapest_anywhere():
    q = p("Куда можно улететь из России дешевле всего?")
    assert q.origin == "Россия" and q.destination == "Куда угодно"
    assert q.flight_budget is None and q.hotel_budget_per_night is None and q.total_budget is None
    assert q.nights_min is None and q.date_from is None


def test_colloquial_and_exact_dates():
    q = p("из Питера в Стамбул с 12 по 17 ноября без пересадок")
    assert q.origin == "Санкт-Петербург" and q.destination == "Стамбул" and q.max_stops == 0
    assert q.date_from == date(2026, 11, 12) and q.nights_min == 5


def test_bare_names_and_mixed_budgets():
    q = p("Москва - Тбилиси на 3-7 ночей, билеты до 15к, отель до 3000 рублей за ночь")
    assert (q.origin, q.destination) == ("Москва", "Тбилиси")
    assert (q.nights_min, q.nights_max) == (3, 7)
    assert q.flight_budget == 15000
    assert q.hotel_budget_per_night == 3000 and q.hotel_budget_currency == "RUB"


def test_exclusions_follow_the_role_they_refer_to():
    q = p("из России куда угодно, но не в Минск и не в Беларусь")
    assert q.origin == "Россия" and q.destination == "Куда угодно"
    assert q.exclude_destinations == ["Минск", "Беларусь"] and q.exclude_origins == []
    q = p("из России, кроме Москвы и Питера, куда угодно")
    assert q.exclude_origins == ["Москва", "Санкт-Петербург"] and q.exclude_destinations == []
    q = p("куда-нибудь кроме Турции и Египта")
    assert q.exclude_destinations == ["Турция", "Египет"]
    q = p("не из Москвы, без Минска")
    assert q.exclude_origins == ["Москва"] and q.exclude_destinations == ["Минск"]


def test_city_lists_and_parts_of_country():
    q = p("из Москвы, Питера или Казани в Стамбул, Тбилиси или Ереван")
    assert q.origin == "Москва, Санкт-Петербург, Казань"
    assert q.destination == "Стамбул, Тбилиси, Ереван"
    assert p("вылет только из европейской части РФ").origin == "Европейская часть России"
    assert p("хочу из-за Урала в Китай").origin == "Азиатская часть России"
    q = p("из любой страны кроме России в Бангкок")
    assert q.origin == "Откуда угодно" and q.exclude_origins == ["Россия"] and q.destination == "Бангкок"


def test_non_place_words_after_triggers_are_ignored():
    q = p("из Москвы без пересадок, не дороже 20 тысяч, на море")
    assert q.origin == "Москва" and q.max_stops == 0
    assert q.exclude_destinations == [] and q.exclude_origins == []


def test_states_and_provinces():
    q = p("хочу из России в Калифорнию за 100к")
    assert q.origin == "Россия" and q.destination == "Калифорния" and q.total_budget == 100000
    q = p("из Москвы во Флориду или Калифорнию на неделю")
    assert q.destination == "Флорида, Калифорния"
    assert p("из Краснодарского края куда угодно").origin == "Краснодарский край"
    notes = parse("из Краснодарского края куда угодно", get_geo(), today=TODAY)[1]
    assert not any(n.startswith("⚠") for n in notes)


def test_big_city_beats_same_named_state():
    assert p("из Москвы в Вашингтон").destination == "Вашингтон"
    assert get_geo().resolve("Вашингтон").kind == "city"
    assert get_geo().resolve("Калифорния").kind == "state"


def test_unknown_place_is_reported_not_silently_ignored():
    q, notes = parse("в Нарнию на неделю", get_geo(), today=TODAY)
    assert q.destination == "Куда угодно"
    assert any("Нарнию" in n and n.startswith("⚠") for n in notes)


def test_trip_length_is_kept_within_2_to_30_nights():
    q = p("из Москвы на выходные")
    assert (q.nights_min, q.nights_max) == (2, 2)
    q, notes = parse("из Москвы на 1 день", get_geo(), today=TODAY)
    assert (q.nights_min, q.nights_max) == (2, 2) and any("2–30" in n for n in notes)
    q = p("из Москвы на 2 месяца")
    assert (q.nights_min, q.nights_max) == (30, 30)
    q = p("из Москвы на 20-40 ночей")
    assert (q.nights_min, q.nights_max) == (20, 30)
    assert (p("из Москвы на месяц").nights_min, p("из Москвы на 3 недели").nights_max) == (30, 21)


def test_at_least_and_at_most_nights():
    assert (p("из Москвы от 4 ночей").nights_min, p("из Москвы от 4 ночей").nights_max) == (4, 30)
    assert (p("из Москвы не меньше 4 дней").nights_min, p("из Москвы не меньше 4 дней").nights_max) == (4, 30)
    assert (p("из Москвы до 5 ночей").nights_min, p("из Москвы до 5 ночей").nights_max) == (2, 5)
    assert (p("из Москвы от 4 до 10 ночей").nights_min, p("из Москвы от 4 до 10 ночей").nights_max) == (4, 10)


def test_minimum_trip_length_in_weeks():
    for text, expected in (("из Москвы от недели", (7, 30)), ("из Москвы не меньше двух недель", (14, 30)),
                           ("из Москвы минимум 2 недели", (14, 30)), ("из Москвы от трёх недель", (21, 30)),
                           ("из Москвы хотя бы от месяца", (30, 30))):
        q = p(text)
        assert (q.nights_min, q.nights_max) == expected, text


def test_prepositions_fillers_and_colloquial_names():
    for text, dest in (("НА Пхукет на неделю", "Пхукет"), ("в Пхукет", "Пхукет"), ("до Пхукета", "Пхукет"),
                       ("из Москвы на остров Пхукет", "Пхукет"), ("из Москвы в город Сочи", "Сочи"),
                       ("на курорт Анталья", "Анталья"), ("в Анталию", "Анталья"), ("слетать в Тай", "Таиланд"),
                       ("в Тайланд", "Таиланд"), ("в Дубаи", "Дубай"), ("на Шри-Ланку", "Шри-Ланка"),
                       ("в штат Флорида", "Флорида")):
        assert p(text).destination == dest, text
    q, notes = parse("на Шри-Ланку", get_geo(), today=TODAY)
    assert not any(n.startswith("⚠") for n in notes)
    assert p("на Самуи").destination != "USM"  # a readable name, not a code


def test_typos_are_corrected_and_reported():
    for text, dest in (("на Пхует", "Пхукет"), ("в Стамбл", "Стамбул"), ("в Тбилиссо", "Тбилиси"),
                       ("в Калифорнею", "Калифорния")):
        q, notes = parse(text, get_geo(), today=TODAY)
        assert q.destination == dest, text
        assert any(n.startswith("Исправил опечатку") for n in notes), text
    assert p("из Масквы в Париж").origin == "Москва"


def test_common_words_after_prepositions_are_not_places():
    for text in ("в среду в Стамбул", "на машине в Казань"):
        q, notes = parse(text, get_geo(), today=TODAY)
        assert not any(n.startswith("Исправил") for n in notes), text
    assert p("в январе на море").destination == "Куда угодно"
    assert p("в отпуск куда-нибудь").destination == "Куда угодно"
