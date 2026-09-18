# EMS-GPT Core — dokumentacja produkcyjna

Status: obowiązująca. Wersja produkcyjna: **0.37.5**.

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
- `ppd_service.py` — decyzje odbiorników po publikacji planu;
- `executor_service.py` — realizacja zatwierdzonych decyzji i ręcznych
  override'ów;
- `ha_gateway_service.py` — dostęp do API Home Assistant;
- `schema_service.py` i `database_service.py` — MariaDB i migracje;
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

## 4. Pompa ciepła `HP_HEAT_DHW`

Automatyczny profil ogrzewania powstaje wyłącznie po spełnieniu wszystkich
warunków:

1. próg pochodzi z ustawienia panelu `night_heating_threshold_c`;
2. istnieją kompletne 24 próbki prognozy dla okresu 00:00–06:00;
3. minimalna prognozowana temperatura jest **ściśle niższa** od progu;
4. slot należy do dziennego okna ogrzewania;
5. slot nie należy do okna `SELL`.

Brak lub niepełność prognozy, temperatura równa progowi albo wyższa oraz błąd
odczytu działają fail-closed: `heat_pump_window=0` i energia `HP_HEAT=0`.

Każdy nowy przebieg planera inicjuje `heat_pump_window=0`. Nie wolno dziedziczyć
wartości `1` z poprzednio opublikowanego planu.

Okno jest takie samo w dni robocze i weekend:

- start najwcześniej o 07:00, zaraz po porannym oknie `SELL`;
- koniec najpóźniej o 19:00 albo wcześniej, przed wieczornym `SELL`;
- `BUY` nie wyznacza początku ani końca okna;
- żaden slot `SELL` nie może uruchomić ogrzewania.

Jeżeli warunek temperatury jest spełniony, energia HP wchodzi do bilansu i może
zwiększyć energię ładowania baterii w wybranym oknie `BUY`. Sam proces HP nie
tworzy okna BUY. Ręczne polecenie operatora zachowuje nadrzędność.

## 5. RCE i bieżące ceny

Import RCE zatwierdza dopiero kompletny zestaw ciągłych slotów. Brak ceny nie
jest zastępowany zerem. Po zatwierdzeniu ceny planowanie może zakończyć się
błędem bez usuwania poprawnie zaimportowanego RCE.

Scheduler odczytuje zakup i sprzedaż z jednego aktywnego rekordu
`ems_gpt_slots`, zaokrągla wartości do trzech miejsc i zapisuje je do dwóch
helperów wymienionych w sekcji 3.

## 6. Publikacja planu i wykonanie

- plan jest publikowany atomowo dopiero po pełnej walidacji;
- aktualny rozpoczęty slot i sloty zamknięte nie są nadpisywane;
- timeout lub błąd pozostawia ostatni kompletny plan;
- ręczny i automatyczny replan korzystają z tej samej serializowanej ścieżki;
- executor pozostaje domyślnie wyłączony i wymaga osobnej decyzji operatora.

## 7. Panel

Panel Ingress pokazuje stan modułów, plan, wykonanie, agregaty godzinowe i
dobowe, analitykę, sugestie oraz diagnostykę. Wybór widocznych kolumn jest
lokalnym ustawieniem przeglądarki i nie zmienia danych ani algorytmu.

## 8. Procedura wydania

Przed scaleniem i publikacją wymagane są:

1. zgodność dokumentacji, kodu i testów;
2. pełny zestaw testów automatycznych;
3. kontrola `git diff --check`;
4. wyszukanie starych i zduplikowanych nazw encji poza archiwum;
5. potwierdzenie wersji w `app.py`, `config.yaml` i changelogu;
6. jawna zgoda operatora na publikację.

Zmiana wersji w plikach nie oznacza publikacji. Dopiero scalone wydanie w
repozytorium jest dostępne dla Home Assistant.
