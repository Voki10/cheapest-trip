"""Server-side texts in Russian and English. tr(key, lang, **params) → the text in that language.

Place names come from the reference data (Russian and English names); site, airline and hotel names
are never translated.
"""

from __future__ import annotations

LANGS = ("ru", "en")


def norm_lang(lang: str | None) -> str:
    return lang if lang in LANGS else "ru"


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def nights(n: int, lang: str) -> str:
    if lang == "en":
        return f"{n} night" + ("" if n == 1 else "s")
    return f"{n} " + plural_ru(n, "ночь", "ночи", "ночей")


def nights_range(lo: int, hi: int, lang: str) -> str:
    return nights(lo, lang) if lo == hi else (f"{lo}–{hi} nights" if lang == "en" else f"{lo}–{hi} ночей")


def stops(n: int, lang: str) -> str:
    if lang == "en":
        return "direct" if n == 0 else f"{n} stop" + ("" if n == 1 else "s")
    return "без пересадок" if n == 0 else f"{n} " + plural_ru(n, "пересадка", "пересадки", "пересадок")


_T: dict[str, tuple[str, str]] = {
    # ── search: input problems ────────────────────────────────────────────────
    "not_found": ("Не нашёл {what}: {names}. Укажите город, страну, регион или код IATA; несколько мест — через запятую.",
                  "Couldn't find {what}: {names}. Enter a city, country, region or IATA code; several places — comma-separated."),
    "what_origin": ("место вылета", "the departure place"),
    "what_dest": ("направление", "the destination"),
    "what_excl_origin": ("что исключить из мест вылета", "what to exclude from departure places"),
    "what_excl_dest": ("что исключить из направлений", "what to exclude from destinations"),
    "period_empty": ("Период поиска закончился или пуст: проверьте даты.", "The search period is over or empty: check the dates."),
    "nights_bounds": ("Длительность поездки — от {lo} до {hi} ночей (минимум ≤ максимум).",
                      "Trip length must be {lo}–{hi} nights (minimum ≤ maximum)."),
    "fx_needed": ("{err}: нельзя привести цены к {cur}", "{err}: cannot convert prices to {cur}"),
    # ── search: how the query was understood ──────────────────────────────────
    "except": ("кроме: {places}", "except: {places}"),
    "when_range": ("вылет {start} – {end}", "departure {start} – {end}"),
    "flexible": ("гибко ({range})", "flexible ({range})"),
    "no_limit": ("без ограничений", "no limit"),
    "per_night": ("{money} / ночь", "{money} / night"),
    "stops_max": ("не более {n}", "at most {n}"),
    "domestic_yes": ("включая внутренние направления", "including domestic destinations"),
    "domestic_no": ("только за границу", "abroad only"),
    "weather_warm": ("тёплое место (средняя t ≥ 18 °C в эти даты год назад)",
                     "warm place (mean ≥ 18 °C on these dates last year)"),
    "weather_any": ("не важно", "any"),
    "fresh_today": ("только цены, которые видели сегодня", "only prices seen today"),
    "fresh_days": ("цены, которые видели на Aviasales не раньше {days} дн. назад",
                   "prices seen on Aviasales within the last {days} days"),
    "yes": ("да", "yes"),
    "no": ("нет", "no"),
    "any_city": ("любой город", "any city"),
    # ── search: progress ──────────────────────────────────────────────────────
    "stage_understand": ("Разбираю условия поиска", "Reading the search conditions"),
    "stage_to": ("Поиск дешёвых вылетов в {n} город(ов) × {m} мес.", "Cheapest flights into {n} cities × {m} months"),
    "stage_from": ("Поиск направлений из {n} город(ов) × {m} мес.", "Destinations from {n} cities × {m} months"),
    "stage_found": ("Найдено направлений: {n}", "Destinations found: {n}"),
    "stage_over_budget": ("{n} направлений дороже бюджета уже в одну сторону",
                          "{n} destinations exceed the budget one way already"),
    "stage_climate": ("Проверка климата для {n} направлений (Open-Meteo)", "Checking climate for {n} destinations (Open-Meteo)"),
    "stage_routes": ("Календари туда/обратно для {n} маршрутов", "Outbound/return calendars for {n} routes"),
    "stage_combine": ("Сочетаю перелёты туда и обратно независимо друг от друга",
                      "Combining outbound and return flights independently"),
    "stage_combined": ("Проверено сочетаний туда+обратно: {n}", "Outbound + return combinations checked: {n}"),
    "stage_hotel_lists": ("Отели: списки в {n} городах", "Hotels: lists in {n} cities"),
    "stage_hotel_rates": ("Цены отелей на даты: {n} вариантов", "Hotel prices for dates: {n} options"),
    "stage_total": ("Считаю полную стоимость поездки", "Calculating the full trip cost"),
    "stage_revalidate": ("Повторная проверка цен для {n} лучших вариантов", "Re-checking prices of the {n} best options"),
    "stage_recheck": ("Перепроверка", "Re-check"),
    "stage_done": ("Готово: {n} направлений, отсортировано по полной стоимости поездки",
                   "Done: {n} destinations, sorted by total trip cost"),
    # ── search: limits of the search ──────────────────────────────────────────
    "limit_dest_cities": ("Проверено {cap} из {total} городов назначения (самые связанные по реальным маршрутам; "
                          "MAX_ORIGIN_CITIES={cap}).",
                          "Checked {cap} of {total} destination cities (best connected by real routes; MAX_ORIGIN_CITIES={cap})."),
    "limit_origin_cities": ("Проверено {cap} из {total} городов вылета (самые связанные по реальным маршрутам; "
                            "MAX_ORIGIN_CITIES={cap}).",
                            "Checked {cap} of {total} departure cities (best connected by real routes; MAX_ORIGIN_CITIES={cap})."),
    "limit_candidates": ("Глубокий разбор: {k} самых дешёвых из {n} найденных направлений (MAX_CANDIDATE_DESTINATIONS={k}).",
                         "In-depth analysis: the {k} cheapest of {n} destinations found (MAX_CANDIDATE_DESTINATIONS={k})."),
    "limit_origins_per_dest": ("Для каждого направления проверены {m} самых дешёвых города вылета "
                               "(MAX_ORIGINS_PER_DESTINATION={m}).",
                               "For each destination the {m} cheapest departure cities were checked "
                               "(MAX_ORIGINS_PER_DESTINATION={m})."),
    "limit_hotels": ("Цены отелей проверены для {n} направлений с самыми дешёвыми перелётами "
                     "(HOTEL_MAX_DESTINATIONS={max}); у остальных итог без отеля.",
                     "Hotel prices were checked for the {n} destinations with the cheapest flights "
                     "(HOTEL_MAX_DESTINATIONS={max}); the others show totals without a hotel."),
    "limit_stale": ("Пропущено {n} цен, которые видели на Aviasales больше {days} дн. назад "
                    "(фильтр свежести; по умолчанию PRICE_MAX_AGE_DAYS).",
                    "Skipped {n} prices last seen on Aviasales more than {days} days ago "
                    "(freshness filter; default PRICE_MAX_AGE_DAYS)."),
    # ── search: warnings ──────────────────────────────────────────────────────
    "provider_failures": ("{label}: {failed} из {total} запросов к провайдеру не удались ({error}). "
                          "Эти варианты не рассматривались.",
                          "{label}: {failed} of {total} provider requests failed ({error}). Those options were skipped."),
    "climate_unavailable": ("Климатические данные недоступны — фильтр «тёплое» не применён.",
                            "Climate data unavailable — the “warm” filter was not applied."),
    "climate_missing": ("Нет климатических данных для {n} направлений — они исключены из «тёплых».",
                        "No climate data for {n} destinations — excluded from “warm”."),
    "climate_note": ("≈{t} °C в эти даты год назад ({source})", "≈{t} °C on these dates last year ({source})"),
    "breakfast_unchecked": ("Источник отелей не сообщает о завтраке — фильтр «Завтрак» не проверялся.",
                            "The hotel source doesn't report breakfast — the “Breakfast” filter was not checked."),
    "recheck_dropped": ("{place} {dates}: {reason} при перепроверке — вариант убран.",
                        "{place} {dates}: {reason} on re-check — option removed."),
    "budget_unverified": ("{n} вариантов дат не показаны: отель для них не проверялся, поэтому нельзя сказать, "
                          "укладываются ли они в общий бюджет.",
                          "{n} date options are hidden: their hotel wasn't checked, so it's unknown whether they fit "
                          "the total budget."),
    "all_over_budget": ("В бюджет не укладывается ни один найденный вариант: самый дешёвый стоит {cheapest}. "
                        "Включите «Показывать варианты дороже бюджета», чтобы увидеть такие варианты, или увеличьте бюджет.",
                        "No option found fits the budget: the cheapest costs {cheapest}. Turn on “Show options above "
                        "budget” to see them, or raise the budget."),
    "nothing_found": ("Провайдер не вернул ни одного варианта, подходящего под условия. "
                      "Попробуйте расширить даты, длительность или бюджет.",
                      "The provider returned no options matching the conditions. Try wider dates, trip length or budget."),
    # ── search: per-trip notes ────────────────────────────────────────────────
    "hotel_error": ("Ошибка провайдера отелей — отель для этих дат не проверен.",
                    "Hotel provider error — no hotel checked for these dates."),
    "hotel_not_checked": ("Отель на эти даты не проверялся (приоритет — более дешёвым вариантам).",
                          "No hotel was checked for these dates (cheaper options had priority)."),
    "above_flight": ("Дороже бюджета: перелёт {total} > {budget}", "Above your budget: flights {total} > {budget}"),
    "above_trip": ("Дороже бюджета: поездка {total} > {budget}", "Above your budget: trip {total} > {budget}"),
    "budget_without_hotel": ("Общий бюджет сравнивался без отеля: данные об отелях для этого города недоступны.",
                             "The total budget was compared without a hotel: no hotel data for this city."),
    "hostel": ("Отель — хостел: цена может быть за место в общем номере. Нужен отдельный номер — включите фильтр "
               "«Отдельный номер».",
               "This is a hostel: the price may be for a bed in a shared room. For a private room, turn on the "
               "“Private room” filter."),
    "w_airports_differ": ("Прилёт в {a}, а обратно вылет из {b} — разные аэропорты.",
                          "You land at {a} but fly back from {b} — different airports."),
    "w_home_airports": ("Вылет из {a}, возвращение в {b}.", "Departure from {a}, return to {b}."),
    "w_no_arrival": ("Провайдер не сообщил время прилёта: ночи посчитаны от даты вылета.",
                     "The provider gave no arrival time: nights are counted from the departure date."),
    "w_night_arrival": ("Прилёт ночью ({time} местного времени).", "Night arrival ({time} local time)."),
    "w_early_return": ("Обратный вылет рано утром ({time}): последнюю ночь в отеле, возможно, не получится провести полностью.",
                       "Early-morning return flight ({time}): you may not get the full last night at the hotel."),
    "leg_out": ("Туда", "Outbound"),
    "leg_ret": ("Обратно", "Return"),
    "w_many_stops": ("{leg}: {stops} — длинный маршрут, проверьте стыковки на странице билета.",
                     "{leg}: {stops} — a long route, check the connections on the ticket page."),
    "w_route_link": ("{leg}: провайдер не дал ссылку на конкретный билет — ссылка ведёт на поиск по маршруту и дате.",
                     "{leg}: the provider gave no link to this exact ticket — the link opens a search for the route and date."),
    "price_changed": ("цена изменилась: {old} → {new} {cur}", "price changed: {old} → {new} {cur}"),
    "ticket_gone": ("билет больше не найден у провайдера", "ticket no longer found at the provider"),
    "recheck_failed": ("Не удалось перепроверить (ошибка провайдера) — показана цена первичного поиска.",
                       "Couldn't re-check (provider error) — the price from the first search is shown."),
    "price_confirmed": ("цена подтверждена", "price confirmed"),
    # ── places ────────────────────────────────────────────────────────────────
    "anywhere": ("любое место", "anywhere"),
    "place_region": ("регион «{label}»", "region “{label}”"),
    "place_state": ("штат / область «{label}»", "state / province “{label}”"),
    "place_country": ("страна «{label}»", "country “{label}”"),
    "place_city": ("город «{label}»", "city “{label}”"),
    "place_airport": ("аэропорт «{label}»", "airport “{label}”"),
    "place_any_of": ("любое из: {places}", "any of: {places}"),
    # ── natural-language parser notes ─────────────────────────────────────────
    "nl_typo": ("Исправил опечатку: «{wrong}» → {right}", "Fixed a typo: “{wrong}” → {right}"),
    "nl_unknown": ("⚠ Не узнал место «{word}» — укажите город, страну, штат/область или регион.",
                   "⚠ Unknown place “{word}” — enter a city, country, state/province or region."),
    "nl_from": ("Откуда: {place}", "From: {place}"),
    "nl_from_anywhere": ("Откуда: откуда угодно (любой город мира)", "From: anywhere (any city in the world)"),
    "nl_from_default": ("Откуда не указано — ищу из «{place}» (можно поменять в форме).",
                        "No departure given — searching from “{place}” (change it in the form)."),
    "nl_to": ("Куда: {place}", "To: {place}"),
    "nl_to_anywhere": ("Куда: куда угодно", "To: anywhere"),
    "nl_to_unspecified": ("Куда: куда угодно (направление не указано — ищу везде)",
                          "To: anywhere (no destination given — searching everywhere)"),
    "nl_excl_from": ("Не вылетать из: {places}", "Not departing from: {places}"),
    "nl_excl_to": ("Исключить направления: {places}", "Excluded destinations: {places}"),
    "nl_nights_clamped": ("Длительность ограничена диапазоном {lo}–{hi} ночей (запрошено {asked}).",
                          "Trip length limited to {lo}–{hi} nights ({asked} requested)."),
    "nl_period_months": ("Период: ближайшие {n} мес.", "Period: next {n} months"),
    "nl_period_month": ("Период: ближайший месяц", "Period: next month"),
    "nl_dates": ("Даты: вылет {start}, обратно {end}", "Dates: out {start}, back {end}"),
    "nl_period_label": ("Период: {label} ({start}–{end})", "Period: {label} ({start}–{end})"),
    "nl_next_month": ("Период: следующий месяц ({month})", "Period: next month ({month})"),
    "nl_next_week": ("Период: следующая неделя (вылет {start}–{end})", "Period: next week (departure {start}–{end})"),
    "nl_anytime": ("Когда: в любое время", "When: anytime"),
    "nl_nights": ("Длительность: {nights}", "Trip length: {nights}"),
    "nl_flexible": ("Длительность: гибко", "Trip length: flexible"),
    "nl_hotel_currency": ("Валюта цены отеля не указана — считаю {cur}.", "No hotel price currency given — assuming {cur}."),
    "nl_hotel_budget": ("Отель: до {money} / ночь", "Hotel: up to {money} / night"),
    "nl_flight_budget": ("Перелёт (туда+обратно): до {money}", "Flights (out + back): up to {money}"),
    "nl_total_budget": ("Вся поездка (перелёт + отель): до {money}", "Whole trip (flights + hotel): up to {money}"),
    "nl_no_flight_budget": ("Бюджет на перелёт не указан — без ограничений, ищу самый дешёвый реальный вариант.",
                            "No flight budget given — no limit, looking for the cheapest real option."),
    "nl_cheapest": ("Цель: минимальная полная стоимость поездки без лимитов по цене.",
                    "Goal: the lowest full trip cost with no price limits."),
    "nl_warm": ("Погода: тёплое место (проверяю по реальным температурам в эти даты год назад)",
                "Weather: a warm place (checked against real temperatures on these dates last year)"),
    "nl_direct": ("Пересадки: только прямые", "Stops: direct only"),
    "nl_stops_max": ("Пересадки: не более {n}", "Stops: at most {n}"),
    "nl_breakfast": ("Отель: с завтраком (источник цен этого не сообщает — проверьте на странице отеля)",
                     "Hotel: with breakfast (the price source doesn't report it — check on the hotel page)"),
    "nl_private": ("Отель: отдельный номер (хостелы исключены)", "Hotel: private room (hostels excluded)"),
    "nl_centre": ("«В центре» — считаю как не дальше 2 км от центра.", "“In the centre” — taken as within 2 km of the centre."),
    # ── providers ─────────────────────────────────────────────────────────────
    "flights_missing": ("Источник авиабилетов не подключён: укажите TRAVELPAYOUTS_TOKEN в .env",
                        "Flight provider not connected: set TRAVELPAYOUTS_TOKEN in .env"),
    "flights_bad_token": ("Travelpayouts отклонил токен (HTTP 401) — проверьте TRAVELPAYOUTS_TOKEN",
                          "Travelpayouts rejected the token (HTTP 401) — check TRAVELPAYOUTS_TOKEN"),
    "aviasales_source": ("Aviasales Data API — цены из реальных поисков пользователей Aviasales за последние дни (кэш)",
                         "Aviasales Data API — prices from real Aviasales users' searches over the last days (cache)"),
    "aviasales_lim_cache": ("Цены Aviasales Data API — это кэш цен из реальных поисков пользователей за последние дни "
                            "(по замеру: половина — за последние сутки, остальные до ~8 дней), а не живой поиск мест. "
                            "Возраст каждой цены показан на билете; фильтр свежести отбрасывает старые. "
                            "Живую цену показывает страница Aviasales по кнопке «Билет туда».",
                            "Aviasales Data API prices are a cache of real users' searches over the last days "
                            "(measured: half within a day, the rest up to ~8 days), not a live seat search. "
                            "Each price shows its age; the freshness filter drops old ones. "
                            "The live price is on the Aviasales page behind the “Outbound ticket” button."),
    "aviasales_lim_live": ("Живой поиск (Aviasales Flight Search API) Travelpayouts открывает только проектам от 50 000 "
                           "пользователей в месяц, поэтому перепроверка лучших вариантов идёт по тому же кэшу.",
                           "Live search (Aviasales Flight Search API) is only open to projects with 50,000+ monthly "
                           "users, so the best options are re-checked against the same cache."),
    "aviasales_lim_segments": ("API не возвращает сегменты перелёта: города пересадок, смену аэропорта и self-transfer "
                               "видно только на странице билета. Число пересадок API сообщает.",
                               "The API returns no flight segments: connection cities, airport changes and "
                               "self-transfer are only visible on the ticket page. The number of stops is reported."),
    "aviasales_lim_price": ("Цена — за одного взрослого в экономе; туда и обратно — два отдельных билета в одну сторону.",
                            "Prices are per adult in economy; outbound and return are two separate one-way tickets."),
    "aviasales_lim_page": ("Один запрос возвращает не больше {n} билетов.", "One request returns at most {n} tickets."),
    "tripcom_no_key": ("Источник отелей не подключён: нет партнёрского доступа к Trip.com API (TRIPCOM_API_KEY). "
                       "Отели не подставляются, итог считается только по перелётам.",
                       "Hotel provider not connected: no partner access to the Trip.com API (TRIPCOM_API_KEY). "
                       "No hotels are added; totals cover flights only."),
    "tripcom_no_client": ("Источник отелей не подключён: ключ Trip.com указан, но клиент их API ещё не подключён — "
                          "нужна партнёрская документация Trip.com.",
                          "Hotel provider not connected: a Trip.com key is set, but their API client isn't built yet — "
                          "Trip.com partner documentation is needed."),
    "xotelo_status": ("Xotelo API (TripAdvisor): Booking.com, Trip.com, Agoda, Expedia…",
                      "Xotelo API (TripAdvisor): Booking.com, Trip.com, Agoda, Expedia…"),
    "xotelo_lim_source": ("Цены — из сравнения TripAdvisor (Booking.com, Trip.com, Agoda, Expedia и др.) через бесплатный "
                          "Xotelo API: цена за номер за ночь на выбранные даты; бронирование — на сайте продавца.",
                          "Prices come from TripAdvisor's comparison (Booking.com, Trip.com, Agoda, Expedia, …) via the "
                          "free Xotelo API: per room per night for the chosen dates; booking is on the seller's site."),
    "xotelo_lim_taxes": ("Налоги и сборы TripAdvisor сообщает не всегда: если их нет в ответе, итог их не включает.",
                         "TripAdvisor doesn't always report taxes and fees: when missing, the total excludes them."),
    "xotelo_lim_breakfast": ("Завтрак источник не сообщает — фильтр «Завтрак» не проверяется.",
                             "The source doesn't report breakfast — the “Breakfast” filter isn't checked."),
    "xotelo_lim_rating": ("Рейтинг — TripAdvisor, по шкале из 5; расстояние — по прямой от центра города.",
                          "Rating is TripAdvisor's, out of 5; distance is a straight line from the city centre."),
    "xotelo_lim_geo": ("Код города TripAdvisor берётся из проверенного справочника или Wikidata; для городов, которых там нет, "
                       "данных об отелях не будет (или добавьте RAPIDAPI_KEY для поиска по названию).",
                       "The TripAdvisor city code comes from a verified list or Wikidata; cities missing there get no hotel "
                       "data (or add RAPIDAPI_KEY to search by name)."),
    "xotelo_down": ("Xotelo недоступен: {err}", "Xotelo unavailable: {err}"),
    "hotels_no_prices_city": ("Нет данных об отелях: в TripAdvisor нет цен на отели этого города.",
                              "No hotel data: TripAdvisor has no hotel prices for this city."),
    "hotels_no_match": ("Нет отелей под фильтры (рейтинг, расстояние, бюджет).",
                        "No hotels match the filters (rating, distance, budget)."),
    "hotels_no_prices_dates": ("Нет данных об отелях: на эти даты у подходящих отелей нет цен в TripAdvisor.",
                               "No hotel data: matching hotels have no TripAdvisor prices for these dates."),
    "hotels_ok": ("Цены на даты из сравнения TripAdvisor", "Prices for the dates from TripAdvisor's comparison"),
    "hotels_taxes": ("Налоги и сборы ({site})", "Taxes and fees ({site})"),
    "hotels_no_location": ("Нет данных об отелях: TripAdvisor-локация города не найдена{hint}",
                           "No hotel data: the city's TripAdvisor location wasn't found{hint}"),
    "hotels_location_hint": (" (добавьте RAPIDAPI_KEY для поиска по названию)", " (add RAPIDAPI_KEY to search by name)"),
    "hotels_no_coords": ("Нет данных об отелях: нет координат города", "No hotel data: the city has no coordinates"),
    # ── site ──────────────────────────────────────────────────────────────────
    "busy_server": ("Сейчас идёт слишком много поисков одновременно. Попробуйте через минуту.",
                    "Too many searches are running right now. Please try again in a minute."),
    "busy_visitor": ("Ваш предыдущий поиск ещё идёт — дождитесь результата.",
                     "Your previous search is still running — please wait for it to finish."),
    "hourly_quota": ("Лимит {n} поисков в час. Следующий будет доступен примерно через {wait} мин.",
                     "Limit of {n} searches per hour. The next one will be available in about {wait} min."),
    "internal_error": ("Внутренняя ошибка: {err}", "Internal error: {err}"),
    "cheapest_alt": ("самый дешёвый на эту дату сейчас: {price}", "cheapest on this date now: {price}"),
    # ── Telegram bot ──────────────────────────────────────────────────────────
    "tg_down": ("📉 Цена снизилась: {old} → <b>{new}</b> ({diff})", "📉 Price dropped: {old} → <b>{new}</b> ({diff})"),
    "tg_up": ("📈 Цена выросла: {old} → <b>{new}</b> ({diff})", "📈 Price went up: {old} → <b>{new}</b> ({diff})"),
    "tg_gone": ("❌ Билет пропал из данных Aviasales (был {old})", "❌ The ticket disappeared from Aviasales data (was {old})"),
    "tg_gone_plain": ("❌ Билет пропал из данных Aviasales", "❌ The ticket disappeared from Aviasales data"),
    "tg_gone_alt": ("Самый дешёвый билет на этот день сейчас: {price}", "Cheapest ticket on this day now: {price}"),
    "tg_back": ("✅ Билет снова в продаже: <b>{price}</b>", "✅ The ticket is back on sale: <b>{price}</b>"),
    "tg_back_was": ("✅ Билет снова в продаже: <b>{price}</b> (до пропажи был {old})",
                    "✅ The ticket is back on sale: <b>{price}</b> (was {old} before it disappeared)"),
    "tg_open": ("Открыть билет", "Open the ticket"),
    "tg_footer": ("Цены — из кэша цен Aviasales, как их только что увидел мониторинг сайта. "
                  "Живую цену и места подтверждает страница бронирования.",
                  "Prices come from the Aviasales price cache, as the site's monitor just saw them. "
                  "The live price and seats are confirmed on the booking page."),
    "tg_hello": ("👋 Я присылаю изменения цен билетов с сайта «Самая дешёвая поездка».\n\n"
                 "Чтобы подключить уведомления, найдите поездку на сайте{url} и нажмите «🔔 Уведомления в Telegram».",
                 "👋 I send price changes for tickets from the “Cheapest trip” site.\n\n"
                 "To turn alerts on, find a trip on the site{url} and press “🔔 Telegram alerts”."),
    "tg_linked": ("✅ Уведомления подключены.\n\nНапишу, когда у билетов, за которыми вы следите на сайте (📌), изменится цена, "
                  "они пропадут из данных Aviasales или вернутся. Каждый билет отслеживается {days} дн. после нажатия «Следить».",
                  "✅ Alerts are on.\n\nI'll write when a ticket you watch on the site (📌) changes price, disappears from "
                  "Aviasales data or comes back. Each ticket is watched for {days} days after you press “Watch”."),
    "tg_list_head": ("Отслеживаю сейчас:", "Watching now:"),
    "tg_list_empty": ("Пока нет отслеживаемых билетов: нажмите «📌 Следить постоянно» у поездки на сайте.",
                      "No watched tickets yet: press “📌 Watch always” on a trip on the site."),
    "tg_list_gone": ("пропал из данных", "gone from the data"),
    "tg_list_more": ("…и ещё {n}", "…and {n} more"),
    "tg_stopped": ("🔕 Уведомления отключены. Включить снова — кнопка «🔔 Уведомления в Telegram» на сайте.",
                   "🔕 Alerts are off. To turn them back on, press “🔔 Telegram alerts” on the site."),
    "tg_help": ("Команды:\n/list — отслеживаемые билеты\n/stop — отключить уведомления",
                "Commands:\n/list — watched tickets\n/stop — turn alerts off"),
    "tg_cmd_list": ("Отслеживаемые билеты", "Watched tickets"),
    "tg_cmd_stop": ("Отключить уведомления", "Turn alerts off"),
}


def tr(key: str, lang: str = "ru", **params) -> str:
    ru, en = _T[key]
    text = en if norm_lang(lang) == "en" else ru
    return text.format(**params) if params else text
