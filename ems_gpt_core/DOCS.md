# EMS-GPT Core — dokumentacja produkcyjna

Status: obowiązująca. Wersja wydania: **0.40.46**.

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
- `ingestion_service.py` — import RCE, jedyny właściciel okien BUY/SELL,
  oraz prognozy PV i pogody;
- `planner_service.py` — transakcyjny planer energii i profil pompy ciepła; prognoza zużycia bazowego zawiera HP_DHW, a tylko zaplanowane HP_HEAT_DHW jest doliczane osobno do bilansu SOC.
- HP_HEAT_DHW kwalifikuje się po co najmniej trzech rzeczywistych odczytach temperatury ogrodowej poniżej skonfigurowanego progu, z okna 19:00 dnia poprzedniego–05:59 dnia ogrzewania. Kwalifikacja następuje w kolejnym replanie po trzecim odczycie; nie czeka do 06:00.
- `ppd_service.py` — publikacja decyzji planera oraz niezależne okna PV→CWU/EV;
- `executor_service.py` — realizacja zatwierdzonych decyzji i ręcznych
  override'ów;
- `ha_gateway_service.py` — dostęp do API Home Assistant;
- `schema_service.py` i `database_service.py` — MariaDB i migracje;
- `observer_service.py` — okresowy audyt planu, wykonania i jakości danych w trybie `SHADOW_READ_ONLY`;
- `api_service.py` oraz `webui.html` — API i panel Ingress.

Usługi otrzymują zależności przez jawne adaptery. Aplikacja korzysta z osobnej
bazy MariaDB `ems_gpt` i nie odczytuje bazy rekordera Home Assistant.

### Jednoznaczni właściciele zapisu

| Dane | Właściciel zapisu | Pozostali konsumenci |
|---|---|---|
| `buy_window`, `sale_window`, `market_window` | Import RCE (`ingestion_service.py`) | planer waliduje; PPD i wykonawca odczytują |
| Prognozy, SOC, import/eksport baterii, przepływ PV do baterii i polityki planu | Planer (`planner_service.py`) | PPD i wykonawca odczytują |
| Podział wolnego PV na CWU/EV/eksport/ograniczenie, rekomendacja i decyzje procesów | PPD (`ppd_service.py`) | API i wykonawca odczytują |
| Polecenia i stan wykonania | Wykonawca (`executor_service.py`) | PPD i diagnostyka odczytują |

PPD wylicza wolną nadwyżkę z prognozy PV, obciążenia bazowego i HP oraz
przepływu PV do baterii. Nie używa poprzednich przydziałów PPD jako wejścia.
Planer nie publikuje kolumn końcowego podziału PV ani rekomendacji; po atomowej
publikacji planu PPD wykonuje własny przebieg. Wykonawca wymaga decyzji
powiązanych z aktualnym `plan_run_id`, więc decyzje z poprzedniego planu nie
autoryzują nowego planu.

Zapisy importu RCE, prognoz PV/pogody i uczenia obciążenia używają wspólnej
blokady ciężkich zadań wraz z planerem i PPD. Replan nie może czytać
częściowo odświeżonego wejścia.

### Kontrola działania

Diagnostyka sprawdza bilans AC każdego otwartego slotu (w tym obciążenie HP),
SOC wymagany przez kontrakt oraz istotny pobór z sieci, którego plan nie
przewidział jako zakupu. `/api/status` udostępnia bilans aktywnego slotu,
łącznie z obciążeniem HP i pozostałym poborem z sieci. Endpoint `/api/plan`
honoruje jawny parametr `limit`.

Endpoint tylko-do-odczytu `/api/executor-diagnostics` służy do analizy BUY
w zakresie do 48 godzin. Wymaga parametrów `from` i `to` w lokalnym czasie HA
(`YYYY-MM-DDTHH:MM:SS` bez przesunięcia strefy). `limit` jest ograniczony do
96 slotów, a `offset` pozwala pobrać kolejną stronę. Każdy wiersz zestawia
opublikowany `soc_charge_target_pct` (oraz wartość zgodnościową
`soc_target_pct`), planowany import i ładowanie, decyzję PPD, zapis wykonania,
ostatnią komendę importu oraz jej wynik i target przekazany do TOU falownika.
Przykład: `/api/executor-diagnostics?from=2026-10-12T08:00:00&to=2026-10-14T08:00:00&limit=96&offset=0`; powtórz z `offset=96`, aby pobrać wcześniejsze sloty zakresu.

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
- `PV_CWU` oraz `PV_EV` są wyznaczane przez PPD po zamrożeniu planu. Planer
  może otworzyć ich wspólne kandydackie okno od następnego slotu po osiągnięciu 100% SOC,
  gdy istnieje istotna planowana nadwyżka PV, nawet poniżej progów uruchomienia
  odbiorników. Prognoza służy wyłącznie do wyznaczenia okna; osobne progi
  uruchomienia sprawdza wykonawca na podstawie rzeczywistego pomiaru.
  Rzeczywista praca zależy od świeżej telemetrii nadwyżki PV i
  automatyki wykonawczej. Wspólne okno zapobiega sytuacji EV=ALLOWED przy
  CWU=BLOCKED; wykonawca stosuje kolejność CWU → EV i histerezę;
  brak danych, SOC poniżej 100% albo zanik nadwyżki wymusza
  wyłączenie w trybie AUTO. Procesy nie są częścią load ani targetu.
- PPD publikuje `SELL_BAT` i `SELL_PV` na dwóch niezależnych osiach:
  polityka `ALLOWED/BLOCKED` oraz stan planowany/efektywny `ON/OFF`.
  `SELL_BAT` korzysta z dotychczasowych bezpiecznych
  skryptów baterii. `SELL_PV` opisuje wyłącznie sprzedaż pozostałej nadwyżki
  PV i nie tworzy polecenia rozładowania baterii.

Obowiązująca kolejność wykorzystania bieżącej produkcji to: najpierw load domu
(w tym HP), następnie ładowanie PV baterii do fizycznego maksimum 100%. Po
osiągnięciu 100% SOC PPD może dopuścić CWU, następnie EV. Pozostała nadwyżka
PV jest sprzedawana tylko przy cenie sprzedaży > 0 PLN/kWh; w przeciwnym razie
produkcja jest ograniczana. Executor bazuje na świeżej telemetrii i po włączeniu
CWU/EV uwzględnia ich pobór, przez co dalsze ładowanie baterii jest naturalnie
zmniejszone. Możliwy przychód ze sprzedaży nie może pominąć CWU ani EV.

Historyczne średnie końcowego SOC 7/14/28 dni są liczone niezależnie z pełnych,
rzeczywiście zamkniętych dób. Doba z brakiem lub `MISSING_OUTAGE` nie jest
próbką. Krótszy zakres odbudowy agregatów nie może ograniczać horyzontu 14/28.
Historyczny zakres 40–45% chroni przed zaniżaniem SOC. Gdy pierwsze okno BUY
nowej lokalnej doby zaczyna się o północy, cel poprzedniej doby jest rozliczany
na końcu tego ciągłego okna, aby nie wymuszać wcześniejszego droższego zakupu.
Do czasu BUY niezależny bilans bezpieczeństwa utrzymuje zapas na prognozowane
zużycie domu/HP do najbliższego PV/BUY. Wymagania są zaokrąglane w górę do
kroku SOC.

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
jest zastępowany zerem. Moduł importu RCE jako jedyny wyznacza i zapisuje
`buy_window`, `sale_window` oraz odpowiadające im `market_window` dla
otwartego horyzontu. Planer tylko odczytuje i waliduje te wartości; nie może
ich ponownie wyliczać ani zapisywać. Po zatwierdzeniu ceny planowanie może
zakończyć się błędem bez usuwania poprawnie zaimportowanego RCE.

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

### Kolejność komend wykonawcy

Komendy OFF kończą się przed uruchomieniem ON. Dispatch wywołuje nazwaną usługę
`script.<nazwa>`, aby czekać na zakończenie sekwencji. `SELL_BAT OFF` nie ustawia
źródła TOU na `Disabled`, gdy globalne ładowanie baterii z sieci jest włączone.
Błąd bateryjnej komendy OFF blokuje nowe bateryjne ON w tym przebiegu.

### Kontrola podtrzymania BUY (0.40.45)

Po dispatch wykonawca w każdym cyklu kontrolnym porównuje rzeczywiste ustawienia
Deye z nadal ważnym poleceniem `BATTERY_IMPORT ON`. Kontrola wymaga bieżącego,
opublikowanego planu i zgodnej wersji polecenia, aktywnego BUY bez SELL,
`BUY_ALLOWED`, dodatniej zaplanowanej energii zakupu, świeżej telemetrii
oraz SOC poniżej dynamicznego `soc_charge_target_pct` (alias `soc_target_pct`).
Obowiązuje aktualna decyzja PPD; aktywne ręczne OFF blokuje wznowienie.
Awaria planera/PPD, wygaśnięcie polecenia i koniec slotu również je blokują.

Jeżeli ustawienia odbiegają od polecenia, wykonawca ponownie ustawia target
wszystkich sześciu programów, ich źródło `Grid` i skrypt importu ON w zakresie
wymaganym przez rozbieżność. Po zapisie odczytuje ustawienia ponownie.
Zdarzenie `battery_import_reconciled` zawiera stan przed/po i `confirmed`;
potwierdzenie dotyczy rejestrów, nie energii faktycznie pobranej z sieci.
Brak potwierdzenia lub błąd zapisu jest rejestrowany i podlega ponownej kontroli
w następnym cyklu, jeśli zakup pozostaje dozwolony.

Ochrona SOC zatrzymuje import synchronicznym skryptem OFF i wyłącza `Grid`
we wszystkich programach. `battery_soc_guard_applied` zapisuje odczyt SOC,
target importu, planowany SOC końca slotu oraz przyczynę zatrzymania.
Brak odczytu SOC nie jest traktowany jako osiągnięcie targetu. W tym samym
przebiegu po zadziałaniu ochrony nie następuje automatyczne wznowienie.

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
5. potwierdzenie wersji w `app.py`, `config.yaml`, README i changelogu;
6. jawna zgoda operatora na publikację.

Zmiana wersji w plikach nie oznacza publikacji. Dopiero scalone wydanie w
repozytorium jest dostępne dla Home Assistant.



## Zakupy i prognoza zużycia w 0.40.31

Wymagane SOC (`soc_required_pct`) wynika z bilansu i rezerw. Opcjonalne sufity
optymalizacji pozwalają przenieść droższy późniejszy zakup do tańszego BUY.
Nie są one targetami wykonawcy. `soc_charge_target_pct` i `soc_target_pct`
zawierają wspólny najwyższy target wybranego ciągłego zakupu, od jego pierwszego
slotu. Ograniczenia BUY/SELL, pojemności, mocy, rezerw i celów dobowych obowiązują
we wszystkich porównywanych wariantach. Zdarzenie `economic_buy_targets`
rozdziela opcjonalne sufity, targety wykonawcze oraz wymagane cele terminowe.

W widoku prognozy zużycia rekordy są wyświetlane od najnowszego. Wykres i
metryki korzystają z chronologicznego zbioru danych; odwrócenie listy jest
wykonywane na kopii i nie zmienia osi wykresu.


Kontrola `VOLUNTARY_GRID_LOAD_WITHOUT_SALE` w 0.40.31 odróżnia pobór domu
podczas rzeczywistego ładowania BUY od dobrowolnego importu bez ładowania.
Ładowanie wymaga przekroczenia `planned_flow_threshold_kwh`; optymalizator
oraz końcowe kontrole stosują ten sam próg. Limit importu obejmuje dom i baterię.

### Prognoza dom/CWU/CO — 0.40.46

Szczegółowy kontrakt: `PLANNER_CONTRACT.md`, sekcja prognozy składników.
Diagnostyka odczytu: `GET /api/consumption-forecast?limit=96` (maks. 500).
Każdy rekord pokazuje partię, wersję modelu, czas uczenia, składniki kWh,
profil CO, liczbę próbek, status i dowody/fallback. Zdarzenia
`component_consumption_forecast` i `planner_consumption_batch` wiążą model
z przebiegiem planera. Aktualizacja wejść i partii odbywa się w jednej transakcji.

Przy aktualizacji ceny RCE w tym samym kwadransie nowy horyzont wymusza nową
partię. Zwykłe kolejne cykle schedulera w tym samym slocie nie przeliczają jej.
Opcjonalny, wycofany klucz `agent_api_token` jest akceptowany wyłącznie przez
schemat Supervisora dla starych instalacji, a `load_options` nadal go usuwa.
Nie przywraca to agenta AI ani uwierzytelniania tokenem.

Weryfikacja produkcji 16.10.2026 przed zmianą: działa 0.40.45; baza, HA i
wykonawca LIVE są połączone. Program 5 SOC=10%, program 6 SOC=30%, oba
charging=Disabled, globalne grid charging=off. Są to potwierdzone stany
odczytu; same w sobie nie dowodzą pełnej historii wykonania przywracania.

Walidacja wydania: 420 testów Core (w tym 13 nowych testów modelu),
32 testy economic-shadow, test interfejsu prognozy zużycia, compileall i
`git diff --check`. Test integracyjny potwierdza wpływ pełnego cyklu CWU
na wymagany SOC przed uzupełnieniem oraz brak wpływu PV_CWU/PV_EV na prognozę.
Nie jest to jeszcze walidacja działania nowego modelu na produkcyjnej bazie.
