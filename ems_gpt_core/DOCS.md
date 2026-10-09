# EMS-GPT Core — dokumentacja produkcyjna

Status: obowiązująca. Wersja przygotowana: **0.39.28**.

Szczegółowe reguły planowania, bilansu i SOC definiuje
[`PLANNER_CONTRACT.md`](PLANNER_CONTRACT.md). Historia zmian znajduje się w
[`CHANGELOG.md`](CHANGELOG.md). Pełny wykaz używanych encji zawiera
[`ENTITY_CATALOG.md`](ENTITY_CATALOG.md). Pliki w `docs/archive/` są materiałem
historycznym i nie stanowią specyfikacji.

## 1. Źródła prawdy

W razie sprzeczności obowiązuje kolejność:

1. `PLANNER_CONTRACT.md` — zachowanie planera i bramy bezpieczeństwa;
2. `DOCS.md` — architektura, integracje i odpowiedzialność encji;
3. testy regresyjne;
4. kod;
5. `CHANGELOG.md` — wyłącznie historia zmian;
6. `docs/archive/` — wyłącznie historia projektu.

Zmiana zachowania wymaga jednoczesnej aktualizacji kodu, testów i właściwego
dokumentu źródłowego.

## 2. Architektura

- `app.py` — kompozycja usług i cykl życia procesu;
- `scheduler_service.py` — zegar, kolejność przebiegów i publikacja bieżących
  cen do Home Assistant;
- `ingestion_service.py` — import RCE oraz prognoz PV i pogody;
- `planner_service.py` — transakcyjny planer energii i profil pompy ciepła;
- `ppd_service.py` — publikacja decyzji planera oraz niezależne okna PV→CWU/EV;
- `executor_service.py` — realizacja zatwierdzonych decyzji i ręcznych
  override'ów;
- `ha_gateway_service.py` — dostęp do API Home Assistant;
- `schema_service.py` i `database_service.py` — MariaDB i migracje;
- `observer_service.py` — okresowy audyt planu, wykonania i jakości danych w trybie `SHADOW_READ_ONLY`;
- `api_service.py` oraz `webui.html` — API i panel Ingress.

Usługi otrzymują zależności przez jawne adaptery. Aplikacja korzysta z osobnej
bazy MariaDB `ems_gpt` i nie odczytuje bazy rekordera Home Assistant.

## 3. Encje Home Assistant i ich właściciele

Core nie tworzy helperów ani równoległych sensorów ceny. Aktualizuje tylko
istniejące encje wskazane poniżej.

| Dane | Jedyna encja zapisu | Zasada |
|---|---|---|
| Bieżąca cena zakupu | `input_number.ems_gpt_cena_zakupu_biezaca` | cena aktywnego slotu z `ems_gpt_slots` |
| Bieżąca cena sprzedaży | `input_number.ems_gpt_cena_sprzedazy_biezaca` | cena tego samego aktywnego slotu |

Obie ceny są wyprowadzane z tego samego slotu. Brak którejkolwiek ceny blokuje
oba zapisy. Zapisy HA są sekwencyjne; błąd jest raportowany i ponawiany przez
scheduler, ponieważ API HA nie oferuje transakcji dla dwóch helperów.

Zabronione jest tworzenie lub aktualizowanie przez Core:

- `sensor.gpt_ems_cena_zakupu` i `sensor.gpt_ems_cena_sprzedazy`;
- dawnych helperów `input_number.optymalizator_deye_*`;
- alternatywnego helpera ceny „początku slotu”.

Core odczytuje istniejącą telemetrię instalacji, między innymi SOC baterii,
temperaturę ogrodu, `weather.dom` i prognozy PV. Nazwy tych encji są częścią
konfiguracji integracji, a nie encjami tworzonymi przez Core.

SOC sześciu programów Deye oraz encja temperatury ogrodu mają jedno źródło
prawdy: opcje dodatku Supervisor. Nie są zapisywane w konfiguracji runtime
panelu aplikacji. Programy 1–5 mają bazowo 10%, a program 6 — 30%.

## 4. Pompa ciepła `HP_HEAT_DHW`

Automatyczny profil ogrzewania powstaje wyłącznie po spełnieniu wszystkich
warunków:

1. próg pochodzi z ustawienia panelu `night_heating_threshold_c`;
2. istnieją co najmniej 3 rzeczywiste zapisy temperatury ogrodu dla okresu
   00:00–06:00;
3. minimalna rzeczywista temperatura jest **ściśle niższa** od progu;
4. slot należy do dziennego okna ogrzewania;
5. slot nie należy do okna `SELL`.

Mniej niż 3 zapisy, temperatura równa progowi albo wyższa oraz błąd odczytu
działają fail-closed: `heat_pump_window=0` i energia `HP_HEAT=0`. Prognoza z
`weather.dom` nie zastępuje rzeczywistego sensora w tej kwalifikacji.

Każdy nowy przebieg planera inicjuje `heat_pump_window=0`. Nie wolno dziedziczyć
wartości `1` z poprzednio opublikowanego planu.

Okno jest takie samo w dni robocze i weekend:

- start najwcześniej o 07:00, zaraz po porannym oknie `SELL`;
- koniec najpóźniej o 19:00 albo wcześniej, przed wieczornym `SELL`;
- `BUY` nie wyznacza początku ani końca okna;
- żaden slot `SELL` nie może uruchomić ogrzewania.

Jeżeli warunek temperatury jest spełniony, energia HP wchodzi do bilansu i może
zwiększyć energię ładowania baterii w wybranym oknie `BUY`. Sam proces HP nie
tworzy okna BUY. Planer publikuje rekomendowany przebieg niezależnie od trybu
wykonawczego. `AUTO` wykonuje rekomendację, `FORCE_ON` ją zastępuje włączeniem,
a `FORCE_OFF` blokuje wykonanie. Override nie przelicza historycznego planu ani
targetu; różnica jest zapisywana jako odchylenie plan–wykonanie.

## 5. Granica odpowiedzialności planer–PPD–executor

- Planer jest jedynym właścicielem rekomendacji `BATTERY_IMPORT`,
  `SELL_BAT` i `HP_HEAT_DHW`, ponieważ ich energia wpływa na bilans, SOC
  oraz target. PPD nie przelicza ich ekonomiki ani kwalifikacji.
- PPD publikuje zamrożoną rekomendację planera jako decyzję `AUTO`, a executor
  nakłada aktywny `FORCE_ON` albo `FORCE_OFF` i tworzy decyzję efektywną.
- Widok Procesy prezentuje osobno rekomendację, stan planowany, tryb sterowania,
  stan efektywny i ilościową energię planu; nie wolno nazywać rekomendacji
  planera stanem rzeczywiście przekazanym do urządzenia.
- Planer nigdy nie odczytuje tabeli decyzji PPD ani override'ów. Dla minionych
  slotów dnia zakłada wykonanie własnego wcześniej opublikowanego planu.
- Rzeczywiste wykonanie i ręczne odstępstwa są domeną tabel wykonania i
  analityki; nie wracają jako ukryte wejście kolejnego planu.
- `PV_CWU` oraz `PV_EV` są wyznaczane przez PPD po zamrożeniu targetu. Planer
  może otworzyć ich wspólne kandydackie okno od następnego slotu po osiągnięciu targetu,
  gdy istnieje istotna planowana nadwyżka PV, nawet poniżej progów uruchomienia
  odbiorników. Prognoza służy wyłącznie do wyznaczenia okna; osobne progi
  uruchomienia sprawdza wykonawca na podstawie rzeczywistego pomiaru.
  Rzeczywista praca zależy od świeżej telemetrii nadwyżki PV i
  automatyki wykonawczej. Wspólne okno zapobiega sytuacji EV=ALLOWED przy
  CWU=BLOCKED; wykonawca stosuje kolejność CWU → EV i histerezę;
  brak danych, SOC poniżej opublikowanego targetu albo zanik nadwyżki wymusza
  wyłączenie w trybie AUTO. Procesy nie są częścią load ani targetu.
- PPD publikuje `SELL_BAT` i `SELL_PV` na dwóch niezależnych osiach:
  polityka `ALLOWED/BLOCKED` oraz stan planowany/efektywny `ON/OFF`.
  `SELL_BAT` korzysta z dotychczasowych bezpiecznych
  skryptów baterii. `SELL_PV` opisuje wyłącznie sprzedaż pozostałej nadwyżki
  PV i nie tworzy polecenia rozładowania baterii.

Obowiązująca kolejność wykorzystania bieżącej produkcji to:
`PV → load (w tym HP) → bateria do targetu → PV_CWU → PV_EV → dalsze
ładowanie baterii / eksport pozostałego PV`.
Wartość możliwego eksportu PV nie może przestawić ani pominąć etapów CWU/EV.
Przed osiągnięciem targetu cała dostępna produkcja pozostaje dla baterii.
Po osiągnięciu targetu nadwyżka dla odbiorów elastycznych jest liczona jako
`PV - load`; bieżące ładowanie baterii nie blokuje CWU/EV, lecz zostaje
naturalnie zmniejszone po ich włączeniu. Moc już działających CWU/EV jest
dodawana z powrotem wyłącznie do oceny histerezy.

Historyczne średnie końcowego SOC 7/14/28 dni są liczone niezależnie z pełnych,
rzeczywiście zamkniętych dób. Doba z brakiem lub `MISSING_OUTAGE` nie jest
próbką. Krótszy zakres odbudowy agregatów nie może ograniczać horyzontu 14/28.
Ważona średnia pomniejszona o 5 punktów procentowych stanowi minimalny SOC
na końcu każdej doby objętej planem, nie tylko ostatniego dnia horyzontu.
Nie jest osobnym wymaganiem przed porannym SELL. Niezależny bilans domu/HP
i możliwości ładowania chroni minimum 15% plus bufor przed PV/BUY
(`soc_replenishment_buffer_pct`, domyślnie 2 p.p.). Wymagania są zaokrąglane
w górę do kroku SOC; potrzeby nocne mogą podnieść wymagane zamknięcie doby.

### AI Observer — audyt w tle

- Wbudowany Observer uruchamia się po przebiegu analityki, nie częściej niż co
  50 minut i wykonuje regułowe kontrole planu, wykonania oraz jakości telemetrii.
- Wyniki i sugestie są zapisywane w historii AI Observera i TODO. Observer
  pozostaje w trybie `SHADOW_READ_ONLY`: nie zmienia planu, PPD, wykonawcy,
  ustawień ani usług Home Assistant.
- Analiza obejmuje eksport przy cenie sprzedaży <= 0, import poza BUY
  (z pominięciem szumu < 0,050 kWh), błędy prognozy PV/zużycia, sloty HP poza
  oknem, SOC zamknięcia doby oraz zgodność planowanego SOC z wymaganym.
- Pojedynczy nietypowy slot nie uruchamia wniosku o systematycznym błędzie;
  wzorce prognozy są oceniane na zagregowanych dobach.
- TODO przechowuje zakres i liczbę analizowanych slotów, metrykę, wartość
  i próg, przykłady z `slot_start`, wniosek oraz zalecaną weryfikację.
- Zewnętrzny AI Agent, jego skrzynka API i Worker zostały usunięte. Wbudowany
  Observer oraz historyczne rekordy w bazie pozostają zachowane.

## 6. RCE i bieżące ceny

Import RCE zatwierdza dopiero kompletny zestaw ciągłych slotów. Brak ceny nie
jest zastępowany zerem. Po zatwierdzeniu ceny planowanie może zakończyć się
błędem bez usuwania poprawnie zaimportowanego RCE.

Scheduler odczytuje zakup i sprzedaż z jednego aktywnego rekordu
`ems_gpt_slots`, zaokrągla wartości do trzech miejsc i zapisuje je do dwóch
helperów wymienionych w sekcji 3.

Cena sprzedaży mniejsza lub równa zero bezwzględnie blokuje sprzedaż baterii.
Kontrola występuje przy tworzeniu okien, w optymalizacji i w walidacji przed
publikacją. Ujemna cena zakupu może pozostać poprawnym sygnałem ekonomicznym,
ale zakup nadal podlega ograniczeniom BUY, pojemności i targetu.

## 7. Publikacja planu i wykonanie

- plan jest publikowany atomowo dopiero po pełnej walidacji;
- aktualny rozpoczęty slot i sloty zamknięte nie są nadpisywane;
- timeout lub błąd pozostawia ostatni kompletny plan;
- ręczny i automatyczny replan korzystają z tej samej serializowanej ścieżki;
- executor pozostaje domyślnie wyłączony i wymaga osobnej decyzji operatora.

## 8. Panel

Panel Ingress pokazuje stan modułów, plan, wykonanie, agregaty godzinowe i
dobowe, analitykę, sugestie oraz diagnostykę. Wybór widocznych kolumn jest
lokalnym ustawieniem przeglądarki i nie zmienia danych ani algorytmu.

## 9. Procedura wydania

Przed scaleniem i publikacją wymagane są:

1. zgodność dokumentacji, kodu i testów;
2. pełny zestaw testów automatycznych;
3. kontrola `git diff --check`;
4. wyszukanie starych i zduplikowanych nazw encji poza archiwum;
5. potwierdzenie wersji w `app.py`, `config.yaml` i changelogu;
6. jawna zgoda operatora na publikację.

Zmiana wersji w plikach nie oznacza publikacji. Dopiero scalone wydanie w
repozytorium jest dostępne dla Home Assistant.
