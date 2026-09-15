# EMS-GPT Core

Kanoniczne, obowiązujące definicje oraz kolejność działania RCE, planera,
replanów, bilansu energii, `soc_target` i `soc_floor` znajdują się w
[`PLANNER_CONTRACT.md`](PLANNER_CONTRACT.md). Kod i testy nie mogą być z nim
sprzeczne.

## Dostosowanie tabel

W panelu dodatku otwórz kartę **Konfiguracja**, a następnie sekcję **Kolumny tabel**.
Wybierz tabelę z listy i zaznacz kolumny, które mają być widoczne. Zmiana jest
zapisywana automatycznie w pamięci lokalnej przeglądarki. Przycisk
**Przywróć domyślne** odtwarza pełny zestaw obserwacyjny danego widoku.

Ustawienie kolumn jest osobne dla każdej przeglądarki i urządzenia. Nie zmienia
danych w MariaDB ani algorytmu planowania.

Niezależny silnik EMS uruchamiany jako lokalna aplikacja Home Assistant.

## Porządkowanie kanonicznej tabeli slotów

Kompaktowanie `ems_gpt_slots` zawsze przebiega w osobnych wydaniach od zmian
algorytmu. Pierwszym etapem jest odczyt `/api/slot-column-audit`, porównujący
enumy, flagi PPD i ilościowe przepływy. Usunięcie pola jest dopuszczalne dopiero
po zerowym wyniku kontroli rozbieżności, trwałym backfillu, przełączeniu
wszystkich odbiorców oraz zachowaniu archiwum. Ilości energii są źródłem prawdy;
flagi prezentacyjne mają być z nich wyliczane. Wykonawca pozostaje wyłączony
podczas całej migracji.

Od wersji 0.33.5 bieżący kontrakt używa jednego `market_window` (`BUY`, `SELL`,
`NEUTRAL`), enumów decyzji oraz ilościowych pól `planned_*_kwh`. Dawne flagi
`grid_*`, `sell_*`, `no_sell_*` i `pv_*_planned` nie są już zapisywane ani
czytane przez planer, API i panel. Pozostają fizycznie wyłącznie na czas
obserwacji zgodności przed osobną migracją usuwającą kolumny.

## Architektura 0.28

- `app.py` — wyłącznie kompozycja usług i cykl życia procesu;
- `schema_service.py` — idempotentna inicjalizacja i migracje schematu MariaDB;
- `planner_service.py` — transakcyjny planer, PPD i optymalizator cykli HP;
- `observer_service.py` — analiza Observera w trybie `SHADOW_READ_ONLY`;
- `diagnostics_service.py` — kontrole diagnostyczne bez zapisu do urządzeń;
- `todo_service.py` — trwały cykl życia sugestii i decyzji operatora;
- `analytics_service.py` — metryki jakości, WAPE, bias i profile uczenia;
- `scheduler_service.py` — wyłącznie zegar i kolejność uruchamiania modułów;
- `api_service.py` — HTTP, Ingress, endpointy odczytu i operacje operatora;
- `executor_service.py` — ustawienia operatora, override'y i chroniony cykl komend;
- `ingestion_service.py` — prognozy PV i pogody oraz import cen RCE;
- `webui.html` — panel Ingress niezależny od kodu serwera.

Usługi otrzymują zależności przez jawne adaptery. Nie importują globalnego stanu
aplikacji i nie mają samodzielnego dostępu do Home Assistant.

Ręczne sterowanie `HP_HEAT_DHW` pozostaje dostępne. Panel nie przyjmuje
oddzielnego czasu pracy: `FORCE_ON` używa parametru `hp_min_cycle_hours`
z centralnej konfiguracji, wspólnego z planerem.

Plan kopii bezpieczeństwa bazy jest dokumentem operacyjnym poza publicznym
repozytorium, ponieważ zawiera szczegóły lokalnej infrastruktury HA i OMV.

## Odpowiedzialność

- CORE: zegar 15-minutowy, telemetria, wznowienie i zamykanie slotów.
- PLANER: transakcyjne przebiegi po tej samej tabeli slotów 15-minutowych:
  `RCE → WINDOWS → LOAD → PV → SLOT_BALANCE → TARGET_COMMITMENT → DISPATCH →
  FLEX_SURPLUS → VALIDATE → PPD`. Każdy przebieg zmienia wyłącznie pola swojej
  odpowiedzialności.
- PPD: polityki sieci, eksportu oraz dostępność PV→CWU i PV→EV.
- ANALYTICS: agregaty godzinowe i dobowe oraz profil zużycia.
- AI OBSERVER: analiza progowa w trybie `SHADOW_READ_ONLY`, potwierdzanie sugestii po 3 kolejnych dniach i audyt decyzji operatora.
- DIAGNOSTICS: cykliczne kontrole danych, planera, wykonania i komend.
- EXECUTOR: chronione sterowanie przez jawnie zaakceptowaną mapę skryptów Home Assistant.

## Dane

Aplikacja używa osobnej bazy MariaDB `ems_gpt` i konta `ems_gpt_app`.
Nie ma dostępu do bazy rekordera Home Assistant. Migracja początkowa 50 tabel
`ems_gpt_*` została wykonana jednokrotnie, a bootstrap pozostaje wyłączony.

## Bezpieczeństwo

- aplikacja wywołuje wyłącznie jawnie dopuszczone skrypty, gdy wykonawca ma potwierdzony tryb LIVE;
- Observer nie wywołuje usług urządzeń i nie zapisuje planu ani PPD;
- aktywny i zamknięte sloty nie są nadpisywane przez replan;
- publikacja planu następuje atomowo dopiero po pełnej walidacji;
- planer ma budżet 120 sekund; timeout lub błąd jednego slotu wycofuje całą
  transakcję i zachowuje ostatni kompletny plan;
- brak cen RCE nie jest zastępowany zerem ani stałą ceną;
- watchdog kontroluje połączenie z MariaDB i pracę procesu.

## Kontrakt energii i SOC 0.34

- Każdy slot cenowy ma dokładnie jeden stan `BUY`, `SELL` albo `NEUTRAL`.
  `BUY` i `SELL` nie mogą się nakładać.
- `soc_target` oznacza energię potrzebną do następnego **faktycznie wybranego**
  uzupełnienia przez PV albo BUY. Jest jednocześnie sufitem ładowania baterii.
  Samo oznaczenie slotu jako `BUY` daje zezwolenie, ale nie tworzy granicy
  bilansu ani obowiązku zakupu.
- Planer najpierw optymalizuje pełny horyzont cenowy, następnie wyprowadza
  target z wybranej ścieżki uzupełnień i ponawia optymalizację do zbieżności.
  Dzięki temu tani wcześniejszy BUY może pokryć zużycie ponad późniejszym,
  drogim oknem BUY, jeżeli pozwalają na to pojemność i moc 5 kW.
- Pełny horyzont oznacza wszystkie ciągłe, niezamknięte sloty z zatwierdzonym
  RCE aż do ostatniego dostępnego slotu, także po przejściu do następnej doby.
  Plan nie jest ograniczany do 96 rekordów. Brak prognozy zużycia w choćby
  jednym slocie blokuje publikację zamiast tworzyć pozornie neutralny odcinek.
- Termin BUY nie obniża targetu w poprzedzających slotach. Wcześniejsze PV
  ładuje baterię do targetu, gdy zastępuje droższą energię sieciową; dopiero
  nadwyżka ponad ten kontrakt może zostać skierowana do sprzedaży/CWU/EV.
- Rozdzielczość prognozy SOC 0,25% nie ogranicza fizycznego przepływu PV.
  Pozostałość mniejsza od kroku jest nadal księgowana jako PV→bateria i nie
  może zostać oznaczona jako eksport, dopóki SOC nie osiągnie targetu.
- Przyszłe PV może odroczyć osiągnięcie targetu tylko wtedy, gdy jego
  zarezerwowana, konserwatywnie skorygowana nadwyżka gwarantuje osiągnięcie
  targetu w terminie. Pozostałe PV jest nadwyżką elastyczną.
- Faktycznie wybrany BUY zamyka poprzedzający most energetyczny. Sloty przed
  nim zachowują energię tylko do początku tego okna oraz część wymagania,
  której wybrane sloty BUY nie zdołają uzupełnić przy limicie mocy i
  sprawności. Target w samym BUY obejmuje odcinek po uzupełnieniu. Analogicznie PV zamyka most dopiero,
  gdy konserwatywna nadwyżka pokrywa całe pozostałe zobowiązanie; częściowe PV
  jedynie je zmniejsza.
- `soc_floor` chroni wyłącznie celową sprzedaż z baterii. Nie jest minimum
  autokonsumpcji i nie może uruchamiać zakupu.
- W slocie sprzedaży publikowany `soc_target` nie może być niższy niż
  `soc_floor`, ale floor nadal nie ogranicza rozładowania na zwykłe zużycie.

### Stała reguła importu i ręcznego replanu

- `BUY` jest decyzją o ładowaniu baterii, nigdy samodzielną decyzją zakupu dla
  odbiorników.
- Gdy SOC osiągnął techniczne minimum, brakująca po PV energia domu jest
  nieuniknionym importem bilansowym. Plan pozostaje wykonalny, ale nie zapisuje
  tego przepływu jako ładowania baterii ani okna BUY.
- Przycisk **Przelicz plan** uruchamia tę samą serializowaną ścieżkę co replan
  automatyczny. Równoległy przebieg nie jest dopuszczony, ostatni poprawny plan
  pozostaje aktywny do czasu atomowej publikacji nowego.
- Kafelki modułów pokazują stan, wykonywaną/ostatnią czynność i czas jej
  aktualizacji. Błąd planera degraduje planer oraz PPD i wskazuje zachowanie
  ostatnich poprawnych decyzji.
- Siatka modułów ma stałe wymiary. Odświeżenie statusu zmienia wyłącznie tekst
  i klasę istniejących pól DOM, bez przebudowy kafelków. RCE jest ostatnim
  kafelkiem siatki modułów.
- Kafelek aktywnego slotu pokazuje lokalny czas bez sufiksu strefy oraz
  planowany bilans `PV + rozładowanie BAT + zakup = zużycie + ładowanie BAT +
  sprzedaż BAT`, wraz z odchyleniem obu stron.
- Oba pola pozostają niezależne. Dla `SELL_BAT` efektywny próg zakończenia
  sprzedaży wynosi `max(soc_floor, soc_target)`: floor jest progiem falownika,
  a target zachowuje energię potrzebną do następnego wybranego PV/BUY. Jeśli
  SOC jest niższy od targetu, planowana sprzedaż baterii wynosi zero.
- Podstawowy bilans slotu to energia PV wykorzystana przez dom/baterię, energia
  baterii i zakup do baterii wobec zużycia oraz ładowania. Sprzedaż nadwyżki PV
  jest przepływem pozabilansowym tego rdzenia i podlega osobnej kontroli
  zachowania energii.
- Nadwyżka po pokryciu domu i rezerwacji targetu jest przydzielana według
  wartości ekonomicznej pomiędzy sprzedaż PV, PV→CWU i PV→EV. Ograniczenie PV
  występuje dopiero po wyczerpaniu wszystkich wykonalnych odbiorów.
- Niepowodzenie bilansu powoduje ponowną ocenę z kolejnym możliwym oknem BUY;
  jeżeli żaden pełny wariant nie jest wykonalny, plan jest odrzucany.
- Zakup sieciowy służy wyłącznie ładowaniu baterii. Zasilanie domu z sieci jest
  dopuszczalne tylko jako jawna, opłacalna ochrona energii przed późniejszą
  sprzedażą; ścieżka bez wykonanej sprzedaży jest ponownie liczona bez wyjątku.

## Panel

Bieżące ceny aktywnego slotu są publikowane parami do Home Assistant jako
`sensor.gpt_ems_cena_zakupu` i `sensor.gpt_ems_cena_sprzedazy`. Źródłem obu
jest ten sam rekord `ems_gpt_slots`; brak jednej ceny blokuje aktualizację całej
pary, aby encje nigdy nie przedstawiały dwóch różnych slotów.

Panel Ingress zawiera status modułów oraz widoki Planer, Wykonanie,
Godzinowe, Dobowe, Analityka, AI Observer, Sugestie / TODO i Diagnostyka.

### Metryki analityczne 0.27

- `quality_score` mierzy kompletność slotów wykonanych przez EMS-GPT Core.
- `metric_confidence_pct` łączy kompletność z długością zebranego okna; pełną
  wiarygodność osiąga po siedmiu dobach danych Core.
- PV WAPE jest liczone tylko w aktywnych slotach produkcji PV, wskazanych przez
  `pv_daylight_slots`.
- Load WAPE i Load bias dotyczą bazowego zużycia domu po odjęciu EV oraz energii
  elektrycznej pompy ciepła/CWU, ponieważ te odbiorniki planer uwzględnia osobno.
- Dla importu i eksportu miarodajne są przede wszystkim `active_mae_kwh` oraz
  `event_f1_pct`; WAPE pozostaje wyłącznie dla ciągłości historycznej.
- `suggested_*_scale` to obserwacyjne, ograniczone do zakresu 0,5–1,5 mnożniki.
  Nie są automatycznie stosowane do planu, PPD ani sterowania.

## Operacje

Zmiana kodu wymaga przebudowania lub restartu wyłącznie aplikacji EMS-GPT Core.
Restart Home Assistant nie jest potrzebny. Zmiana użytkowników MariaDB wymaga
jednorazowego restartu aplikacji MariaDB.

## Pakiet 0.21.0

Wersja 0.21.0 przygotowuje brakujące kontrakty do odbioru etapowego. Wszystkie
funkcje wykonawcze pozostają bezpiecznie nieaktywne: `executor_enabled=false`,
`executor_dry_run=true`. Zmiana tych wartości nie jest częścią zwykłej
aktualizacji i wymaga odbioru opisanego w `VALIDATION_STAGES.md`.

Nowe endpointy:

- `GET /api/overrides`, `POST /api/process/override`;
- `GET /api/commands`, `POST /api/executor/stage`;
- `POST /api/connector/ack`;
- `GET /api/process-execution`;
- `GET /api/todo`.
- `POST /api/todo/review` z decyzją `ACCEPTED`, `REJECTED` albo `RESOLVED`.

Bez potwierdzonego trybu znaku `sensor.inverter_battery_power` należy pozostawić
Domyślnie `direct_battery_power_mode=discharge_positive`, zgodnie ze znakiem mocy Deye
(wartość dodatnia oznacza rozładowanie, ujemna ładowanie). Zachowana wartość
`legacy_helpers` jest automatycznie migrowana do tego trybu i nie odczytuje helperów V1/V2/V3.

RCE bieżącego slotu jest pobierane bezpośrednio z `ems_gpt_slots`; aplikacja nie wymaga
sensora `sensor.ems_gpt_rce_pse_current`. Po awarii aplikacja domyślnie odbudowuje
SLOT/HOUR/DAILY z ostatnich 7 dni. Zakres można zmienić w karcie Konfiguracja parametrem
„Zakres odtwarzania po awarii [dni]”.
# Kontrakt importu RCE i planera

- Import RCE jest zakończony wyłącznie po walidacji kompletnego dnia cenowego
  (`rows == expected`, standardowo `96/96`; dni zmiany czasu mogą mieć inną
  liczbę slotów).
- Kompletność cen i wykonalność planu są niezależnymi stanami. Zatwierdzony
  import pozostaje poprawny, gdy późniejsze odświeżenie prognoz lub planer
  zgłosi błąd.
- Błąd etapu zależnego ma status `planner=ERROR` i zdarzenie
  `rce_dependent_cycle_failed`. Nie wolno raportować go jako błędu importu RCE,
  ponawiać z tego powodu pobierania ani zastępować kompletnego dnia częściowym.
- Plan może zostać opublikowany dopiero po kompletnym imporcie RCE i własnej
  walidacji wykonalności.
