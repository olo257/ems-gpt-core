# EMS-GPT Core 0.20.0 — dokumentacja produkcyjna i przekazanie prac

**Stan dokumentu:** 2026-09-09  
**Cel:** umożliwić kolejnemu czatowi bezpieczną i natychmiastową kontynuację migracji EMS-GPT do niezależnej aplikacji Home Assistant.  
**Aktualna wersja aplikacji:** `0.20.0`  
**Slug Supervisor:** `local_ems_gpt_core`

## 1. Najważniejsze ustalenie architektoniczne

EMS-GPT Core jest niezależną aplikacją uruchamianą jako lokalny dodatek Home Assistant Supervisor na Raspberry Pi 5 (`aarch64`). Nie należy oznaczać aplikacji jako V3 ani V4. Produkcyjna integracja V3 działa osobno i pozostaje nietknięta do czasu wdrożenia oraz odbioru konektora wykonawczego.

Aplikacja jest docelowym „mózgiem” EMS:

- pobiera dane z Home Assistant;
- przechowuje własne dane w MariaDB;
- importuje ceny RCE z PSE;
- pobiera prognozy Open-Meteo;
- buduje Planer i PPD;
- zamyka wykonanie slotów;
- prowadzi analitykę i diagnostykę;
- publikuje panel przez Ingress.

Home Assistant pozostaje warstwą urządzeń, zabezpieczeń i — po zbudowaniu konektora — wykonywania poleceń. Zwykła aktualizacja aplikacji wymaga restartu wyłącznie EMS-GPT Core, a nie HA Core ani całego hosta.

## 2. Granice bezpieczeństwa

Aktualny wykonawca urządzeń jest świadomie wyłączony i raportuje `CONNECTOR_REQUIRED`. Aplikacja planuje i obserwuje bez ograniczenia typu SHADOW, ale nie wysyła poleceń do urządzeń. Dzięki temu może działać równolegle z produkcyjnym V3 bez konfliktu sterowania.

Bezwzględne zasady:

- nie modyfikować programów bezpieczeństwa SOC 1–6;
- nie uruchamiać równoległego sterowania tymi samymi urządzeniami z aplikacji i V3;
- nie włączać poleceń do urządzeń bez kompletnego konektora, TTL, potwierdzeń i blokad lokalnych;
- nie restartować HA Core ani hosta przy aktualizacji aplikacji;
- nie nadpisywać aktywnego ani zamkniętego slotu podczas replanu;
- nie zastępować brakującej ceny RCE zerem ani stałą ceną;
- nie ujawniać hasła MariaDB w dokumentacji, logach ani kodzie;
- publikować plan atomowo dopiero po pełnej walidacji.

Ogólna zgoda użytkownika obejmuje dalsze modyfikacje aplikacji oraz jej `config.yaml`. Mechanizm dostępu może mimo to wymagać ponownego, precyzyjnego potwierdzenia operacji chronionej.

## 3. Środowisko i lokalizacje

| Element | Wartość |
| --- | --- |
| Platforma | Home Assistant OS, Raspberry Pi 5, `aarch64` |
| Aplikacja | `EMS-GPT Core` |
| Slug zainstalowanej aplikacji | `local_ems_gpt_core` |
| Panel | Ingress, port wewnętrzny `8099` |
| Watchdog | `http://[HOST]:[PORT:8099]/health` |
| Start | `startup: application`, `boot: auto` |
| Lokalny katalog roboczy | `/workspace/scratch/ebe82aadcf99/ems_gpt_core` |
| Staging na HA | `/share/ems-gpt-core` |
| Źródło lokalnego dodatku na HA | `/addons/ems_gpt_core` |
| Instalator pomocniczy | Advanced SSH & Web Terminal, slug `a0d7b954_ssh` |
| MariaDB host | `core-mariadb:3306` |
| Baza | `ems_gpt` |
| Użytkownik DB | `ems_gpt_app` |
| Strefa czasowa | `Europe/Warsaw` |
| Długość slotu | 15 minut, 96 slotów/dobę |

Hasło bazy jest zachowane w opcjach Supervisor. W źródłowym `config.yaml` celowo występuje wyłącznie znacznik `__DB_PASSWORD__`. Podczas aktualizacji nie wolno zastępować rzeczywistego hasła tym znacznikiem.

## 4. Pliki źródłowe

W katalogu `ems_gpt_core/` znajdują się:

- `app.py` — aplikacja, API, scheduler, logika planowania, PPD, telemetria i panel;
- `config.yaml` — manifest i techniczna konfiguracja dodatku;
- `CHANGELOG.md` — historia wdrożonych funkcji;
- `DOCS.md` — skrócona dokumentacja dodatku;
- `Dockerfile`, `run.sh` — uruchomienie kontenera;
- `icon.png`, `logo.png` — identyfikacja wizualna.

Dziedziczony changelog V3 dostępny był jako `project_sources/01-EMS-GPT_CHANGELOG.md`. Służy jako materiał historyczny do migracji mechanizmów, a nie jako kod produkcyjny nowej aplikacji.

## 5. Baza danych i trwałość

Aplikacja korzysta z osobnej bazy MariaDB `ems_gpt` i konta `ems_gpt_app`. Nie ma dostępu do bazy rekordera Home Assistant. Początkowo zmigrowano 50 tabel `ems_gpt_*` 1:1. Bootstrap ze źródła jest wyłączony (`bootstrap_from_source: false`) i nie powinien zostać przypadkowo ponowiony.

Zasady zapisu:

- wszystkie czasy interpretować w `Europe/Warsaw`;
- znacznik ceny RCE oznacza koniec kwadransa;
- `SOC end` oznacza stan na końcu slotu;
- zamykanie slotu jest idempotentne;
- po restarcie aplikacja odzyskuje przerwane przebiegi i uzupełnia możliwe braki;
- najpierw utrwalać Wykonanie, potem publikować kolejny Planer;
- publikacja nowego planu musi być transakcyjna i kompletna;
- moduły analityczne i diagnostyczne nie powinny samodzielnie modyfikować planu produkcyjnego.

## 6. Aktualny stan modułów

| Moduł | Stan | Uwagi |
| --- | --- | --- |
| CORE / scheduler | działa | zegar 15-minutowy, recovery po restarcie |
| MariaDB | `CONNECTED` | osobna baza `ems_gpt` |
| Odczyt HA | `CONNECTED` | REST/API dodatku |
| RCE PSE | działa | pełne 96 cen, automatyczne ponowienia |
| Open-Meteo | działa | prognozy PV i pogody |
| Planer | działa | pełny horyzont, publikacja atomowa |
| PPD | działa | zoptymalizowany przebieg liniowy O(n) |
| Wykonanie | działa | szczegóły energii, SOC, PV1/PV2 i HP |
| Analityka | działa | WAPE, profile, jakość slotów |
| Diagnostyka | działa | kompletność, świeżość, duplikaty, pokrycie |
| EXECUTOR | `CONNECTOR_REQUIRED` | brak poleceń do urządzeń |

Ostatni sprawdzony stan przed przekazaniem: aplikacja `RUNNING`, baza i wejście HA połączone, brak `last_error`. Diagnostyka zgłaszała `OK`, 0 alertów, pełny horyzont 96 oraz 96 cen PSE. W ostatnich 24 godzinach było około 95 zamkniętych slotów, średnie pokrycie telemetrii około 96,14%, jeden slot poniżej 80% i brak duplikatów.

## 7. Zaimplementowane mechanizmy według wersji

### 0.6–0.11

- kontener Supervisor, Ingress, MariaDB i migracja 50 tabel;
- zegar slotów, telemetria, restart recovery;
- etapowy planer RCE → FORECAST → WINDOWS → SOC → PPD → VALIDATE;
- dynamiczne podłogi i cele SOC;
- import RCE PSE z dynamiczną marżą;
- wykonanie, agregacja godzinowa i dobowa;
- profile zużycia 7 × 24 × 4 = 672;
- sezonowe profile produkcji PV;
- macierz sześciu procesów: `BATTERY_IMPORT`, `BATTERY_EXPORT`, `PV_CWU`, `PV_EV`, `MANUAL_CIRCULATION`, `HP_DHW`;
- Open-Meteo zamiast Forecast.Solar.

### 0.12–0.16

- konfiguracja marży zakupu, sprawności ładowania i rozładowania oraz degradacji;
- konfiguracja pojemności, minimalnego SOC, mocy baterii i progów procesów;
- ustawienia runtime w `/data/runtime-settings.json` bez restartu aplikacji;
- dziennik zmian ustawień w MariaDB;
- karta `Konfiguracja` jako ostatnia zakładka panelu, przygotowana do rozbudowy.

### 0.17–0.18

- szczegółowe Wykonanie z energią EV/CWU, eksportem, liczbą próbek i pokryciem;
- normalizacja W/kW/MW oraz odtwarzanie szczegółów z 24 godzin;
- poziomy pasek przewijania, zamrożony nagłówek i kolumna `Slot`;
- rekomendacja w drugiej kolumnie;
- pola PPD 1/0 prezentowane jako `TAK/NIE`;
- po kompletnym imporcie 96 RCE: Open-Meteo → profile zużycia → Planer → PPD;
- identyczny ciąg po ręcznym odświeżeniu RCE;
- niepełny import kończy się `WAITING_FOR_96_RCE_ROWS`.

### 0.19–0.20

- bezpośrednie pomiary PV1 i PV2 oraz niezależne WAPE;
- szczegóły pompy ciepła: zasilanie, powrót, delta T, częstotliwość, prąd i przepływ;
- SOC start/min/end/delta w każdym slocie;
- temperatura rzeczywista z `sensor.klimat_w_ogrodzie_temperature`;
- całkowanie energii pobranej i użytkowej dla ogrzewania, CWU i chłodzenia;
- COP dla każdego trybu i łącznie;
- tryb HP, licznik uruchomień i godziny pracy;
- rozszerzona tabela Wykonanie.

## 8. Encje źródłowe Home Assistant

### Źródła bezpośrednie

```text
sensor.inverter_pv1_power
sensor.inverter_pv2_power
sensor.klimat_w_ogrodzie_temperature
sensor.panasonic_heat_pump_main_main_outlet_temp
sensor.panasonic_heat_pump_main_main_inlet_temp
sensor.panasonic_heat_pump_main_compressor_freq
sensor.panasonic_heat_pump_main_compressor_current
sensor.panasonic_heat_pump_main_pump_flow
sensor.panasonic_heat_pump_main_heat_power_consumption
sensor.panasonic_heat_pump_main_heat_power_production
sensor.panasonic_heat_pump_main_dhw_power_consumption
sensor.panasonic_heat_pump_main_dhw_power_production
sensor.panasonic_heat_pump_main_cool_power_consumption
sensor.panasonic_heat_pump_main_cool_power_production
sensor.panasonic_heat_pump_main_operations_counter
sensor.panasonic_heat_pump_main_operations_hours
```

### Prognozy PV Open-Meteo

```text
sensor.open_meteo_pv1_e_energy_production_today_remaining
sensor.open_meteo_pv2_w_energy_production_today_remaining
sensor.open_meteo_pv1_e_energy_production_tomorrow
sensor.open_meteo_pv2_w_energy_production_tomorrow
```

Należy przed użyciem zawsze potwierdzić dokładne nazwy wariantów `tomorrow` dostępnych w HA — powyższe nazwy wynikają z obecnego kontraktu aplikacji.

### Pozostałe zależności pośrednie po V3

```text
sensor.ems_gpt_moc_ladowania_baterii_dokladna
sensor.ems_gpt_moc_rozladowania_baterii_dokladna
sensor.ems_gpt_rce_pse_current
```

Istnieje kandydat bezpośredni `sensor.inverter_battery_power`, ale nie wolno przełączyć telemetrii bez potwierdzenia znaku i kierunku. Zaobserwowano stan `515 W` i atrybut `−x: -515`; znaczenie musi zostać sprawdzone w realnych cyklach ładowania i rozładowania.

## 9. Aktualne parametry runtime

Ostatnio sprawdzone wartości:

| Parametr | Wartość |
| --- | ---: |
| Marża zakupu | 0,60 PLN/kWh |
| Sprawność ładowania | 0,98 |
| Sprawność rozładowania | 0,98 |
| Degradacja baterii | 0,05 PLN/kWh |
| Pojemność baterii | 15 kWh |
| Minimalny SOC | 15% |
| Maksymalna moc baterii | 5 kW |
| Minimalna marża arbitrażu | 0 PLN/kWh |
| Próg P80 spadku SOC | 60% |
| Próg PV→CWU | 2,0 kW |
| Próg PV→EV | 1,5 kW |

Źródłem prawdy jest jednak `GET /api/settings` oraz `/data/runtime-settings.json`, ponieważ użytkownik może zmieniać ustawienia z panelu bez restartu.

## 10. Reguły biznesowe do zachowania

- Cena zakupu = RCE + konfigurowalna marża.
- Ujemne RCE może pozwalać na zakup.
- Nie kupować w oknie sprzedaży.
- Nie sprzedawać PV, gdy cena sprzedaży jest mniejsza lub równa zero.
- Koszt odzyskania energii uwzględnia sprawność ładowania, sprawność rozładowania, degradację i minimalną marżę.
- Priorytet nadwyżki: bateria → CWU → EV.
- Progi PV→CWU i PV→EV są niezależne.
- EV nie ma dziennego limitu 15 kWh.
- `MANUAL_CIRCULATION`: cykl 45 minut.
- `HP_DHW`: dni robocze 06:00–07:30, weekendy 08:00–09:00.
- Podczas automatycznego cyklu HP pompa obiegowa działa przez cały cykl.
- Eksport rzeczywisty nie może być arbitralnie dzielony na PV i baterię; bez pewnego trybu falownika pozostaje `UNRESOLVED`.

## 11. API aplikacji

### Odczyt

```text
GET /health
GET /api/status
GET /api/settings
GET /api/plan
GET /api/execution
GET /api/hourly
GET /api/daily
GET /api/runs
GET /api/analytics
GET /api/diagnostics
GET /api/processes
```

### Operacje

```text
POST /api/settings
POST /api/planner/run
POST /api/rce/refresh?day=YYYY-MM-DD
POST /api/forecast/refresh
POST /api/analytics/run
POST /api/diagnostics/run
```

Widoki panelu: `Planer`, `Wykonanie`, `Godzinowe`, `Dobowe`, `Procesy`, `Analityka`, `Diagnostyka`, `Konfiguracja`. Konfiguracja pozostaje ostatnią kartą, ponieważ liczba ustawień będzie rosła.

## 12. Ostatnie zweryfikowane dane

Import RCE dla 2026-09-10 zakończył się kompletem 96 wierszy, a planer zaakceptował pełny horyzont 96 slotów. Prognozy Open-Meteo na kolejny dzień wynosiły około:

- PV1: 17,942 kWh;
- PV2: 11,87575 kWh.

Zweryfikowany slot wersji 0.19.0, 14:15–14:30:

| Pole | Wynik |
| --- | ---: |
| PV1 | 0,580 kWh |
| PV2 | 0,49075 kWh |
| PV razem | 1,09015 kWh |
| HP zasilanie / powrót | 37,75 / 36,00 °C |
| Delta T | 1,75 °C |
| Sprężarka | 0 Hz |
| Przepływ | 0,13 |
| HP działa | NIE |
| EV | 0,532595 kWh |
| Próbki | 15 |
| Pokrycie | 100% |

WAPE: PV1 43,647%, PV2 59,163%, PV łącznie 40,527%. Niska ogólna ocena jakości historycznej jest spodziewana, ponieważ zmigrowane stare sloty nie posiadają kompletu próbek aplikacji. Ostatnie sloty mają dobre pokrycie.

## 13. Najpilniejszy krok po przejęciu

Wersja 0.20.0 wystartowała i przeszła healthcheck, lecz nie został jeszcze zweryfikowany pierwszy kompletny, zamknięty slot zawierający wszystkie nowe pola. Kolejny czat powinien najpierw:

1. sprawdzić, czy od zamknięcia slotu wersji 0.20.0 minęło wystarczająco dużo czasu;
2. odczytać `/api/execution` i rekordy szczegółowe MariaDB;
3. potwierdzić `SOC start/min/end/delta`;
4. potwierdzić temperaturę z `sensor.klimat_w_ogrodzie_temperature`;
5. zweryfikować energie HP dla heat/DHW/cool i ich jednostki;
6. policzyć kontrolnie COP oraz sprawdzić zachowanie przy energii wejściowej równej zero;
7. sprawdzić tryb HP, licznik uruchomień i godziny pracy;
8. upewnić się, że tabela Wykonanie prezentuje wartości bez błędów JavaScript/API.

Nie należy uznawać punktu za wykonany tylko na podstawie statusu `RUNNING`.

## 14. Kolejka dalszego wdrożenia

### P0 — walidacja 0.20.0

- wykonać kroki z sekcji 13;
- naprawić ewentualne błędy jednostek, integracji lub `NULL`;
- dopisać wyniki odbioru do `CHANGELOG.md` i niniejszego handoffu.

### P1 — usunięcie pozostałych zależności od V3

- ustalić znak i kierunek `sensor.inverter_battery_power` w ładowaniu, spoczynku i rozładowaniu;
- zastąpić dwa helpery dokładnej mocy baterii źródłem bezpośrednim;
- opcjonalnie zastąpić `sensor.ems_gpt_rce_pse_current` bezpośrednim źródłem dla telemetrii bieżącej ceny;
- potwierdzić brak regresji energii ładowania/rozładowania i bilansu slotu.

### P2 — optymalizacja PPD

- dodać sensowną wartość końcowego SOC na końcu horyzontu;
- dopracować zachowanie przy SOC 90–100% i pozostałej prognozie PV;
- dodać dynamiczny wieczorny cel SOC, orientacyjnie około 60% o 20:00, ale wyliczany z ryzyka i prognozy;
- uwzględnić niepewność prognozy PV i zużycia;
- rozszerzyć uczenie profilu domu i pompy ciepła;
- obsłużyć sytuację, gdy EV jest odłączone mimo dozwolonego okna;
- zachować liniową złożoność przebiegu PPD.

### P3 — procesy operatorskie

- dodać ręczne przełączniki, blokady i statusy sześciu procesów;
- prowadzić historię decyzji, przyczyn, ważności i przyszłych potwierdzeń;
- porównywać plan procesu z wykonaniem;
- rozbudować widok `Procesy` o czytelne karty operatorskie;
- nadal nie wywoływać urządzeń bez konektora.

### P4 — analityka i diagnostyka

- porównanie plan–wykonanie per slot i proces;
- odchylenia PV1/PV2 z pętlą uczenia profili;
- pełniejszy bilans energii i finansów w PLN;
- rozbudowane widoki godzinowe i dobowe;
- rozwijane szczegóły alertów diagnostycznych;
- obserwator AI i dobowa lista TODO dopiero po ustabilizowaniu danych bazowych.

### P5 — konektor wykonawczy do HA

Konektor jest ostatnim dużym elementem przed produkcją. Minimalny kontrakt polecenia:

```json
{
  "slot": "2026-09-09T08:30:00+02:00",
  "process": "BATTERY_IMPORT",
  "decision": "ON",
  "expires_at": "2026-09-09T08:46:00+02:00",
  "plan_version": 1742,
  "command_id": "unikalny-identyfikator"
}
```

Konektor musi zapewnić:

- weryfikację bieżącego slotu i `expires_at`;
- idempotencję `command_id`;
- lokalne blokady SOC, stanu urządzeń i ręcznego override;
- potwierdzenie przyjęcia oraz faktycznego wykonania;
- audyt w MariaDB;
- recovery po restarcie bez ponawiania wygasłych poleceń;
- bezpieczny fallback przy utracie łączności;
- ochronę programów SOC 1–6;
- mechanizm uniemożliwiający równoległe sterowanie z V3.

### P6 — odbiór produkcyjny

- test restartu EMS-GPT Core;
- test restartu HA Core i całego hosta;
- test utraty MariaDB, HA API i konektora;
- pełna doba 96 slotów bez luk, duplikatów i skoków;
- walidacja strefy czasowej i zmian czasu;
- uruchamianie procesów pojedynczo;
- kolejność rekomendowana: PV→CWU, PV→EV, import baterii, pozostałe procesy, sprzedaż baterii na końcu;
- pełna kopia przed aktywacją sterowania;
- udokumentowany rollback do trybu bez poleceń.

## 15. Znane ograniczenia i zakazy interpretacyjne

- Źródło eksportu PV kontra bateria pozostaje `UNRESOLVED`, dopóki brak wiarygodnej informacji o trybie falownika.
- Bieżąca temperatura z ogrodu jest dostępna, lecz nie należy wymyślać bieżącego zachmurzenia i opadów, jeśli `weather.dom` ich nie udostępnia jako danych aktualnych.
- Historyczne WAPE i quality score są obciążone niepełnymi danymi sprzed migracji; analizować osobno okres po wdrożeniu aplikacji.
- Nie utożsamiać rekomendacji procesu z wykonaniem urządzenia przed powstaniem konektora.
- Nie włączać `bootstrap_from_source` bez planu migracji i kopii bazy.
- Nie zmieniać definicji znacznika RCE ani semantyki `SOC end`.

## 16. Bezpieczna procedura wdrożenia kolejnej wersji

1. Zmienić kod lokalnie w `ems_gpt_core/` i podnieść wersję w `config.yaml`.
2. Uzupełnić `CHANGELOG.md`.
3. Sprawdzić składnię Python i manifest YAML; wykonać lokalne testy istotnych funkcji.
4. Umieścić `app.py`, `config.yaml` i `CHANGELOG.md` w `/share/ems-gpt-core`.
5. Tymczasowo ustawić w Advanced SSH & Web Terminal polecenia kopiujące pliki do `/addons/ems_gpt_core` i wykonujące `ha store reload`.
6. Zrestartować wyłącznie aplikację terminala i sprawdzić wynik.
7. Natychmiast wyczyścić `init_commands` terminala i ponownie zrestartować wyłącznie terminal.
8. Zaktualizować `local_ems_gpt_core` przez Supervisor.
9. Nie nadpisywać rzeczywistego hasła DB placeholderem z repozytorium.
10. Sprawdzić `/health`, `/api/status`, logi, status MariaDB i HA input.
11. Sprawdzić odpowiednie widoki API oraz co najmniej jeden pełny zamknięty slot.
12. Restartować tylko EMS-GPT Core, chyba że konkretny test odbiorowy jawnie dotyczy HA/hosta.

## 17. Definicja ukończenia pakietu zmian

Pakiet można uznać za wdrożony dopiero, gdy:

- wersja jest widoczna w Supervisor;
- kontener ma stan `RUNNING` i watchdog jest zdrowy;
- DB oraz odczyt HA są `CONNECTED`;
- endpointy zwracają poprawny JSON;
- migracje są idempotentne;
- zamknięty slot zawiera oczekiwane wartości i jednostki;
- brak nowych luk i duplikatów;
- produkcyjny V3 oraz SOC 1–6 nie zostały zmienione;
- executor nadal jest wyłączony, chyba że odbierany jest kompletny konektor;
- changelog opisuje zmianę i wynik walidacji.

## 18. Gotowy prompt dla kolejnego czata

Skopiuj poniższy tekst wraz z tym dokumentem:

> Kontynuuj autonomicznie migrację EMS-GPT Core na podstawie dokumentu `EMS-GPT-CORE-HANDOFF-PRODUKCYJNY-0.20.0.md`. Najpierw zweryfikuj aktualny stan aplikacji i pierwszy kompletny slot wersji 0.20.0 — nie zakładaj, że nowe pola SOC i HP są poprawne tylko dlatego, że healthcheck działa. Następnie realizuj kolejkę od P1, zachowując istniejącą logikę, MariaDB i panel. Aktualizuj wyłącznie aplikację EMS-GPT Core; nie restartuj HA Core ani hosta i nie modyfikuj produkcyjnego V3 ani programów SOC 1–6. Aplikacja ma działać niezależnie, lecz nie może wysyłać poleceń do urządzeń do czasu wdrożenia kompletnego konektora z TTL, blokadami, potwierdzeniami i audytem. Po każdym pakiecie podnieś wersję, uzupełnij changelog, wdroż aplikację i zweryfikuj dane w zamkniętym slocie. Nie zapisuj ani nie ujawniaj hasła MariaDB; zachowaj wartość obecną w opcjach Supervisor.

## 19. Priorytet decyzji dla następnego czatu

Jeżeli zastany stan różni się od tego dokumentu, pierwszeństwo mają kolejno:

1. rzeczywisty stan Supervisor i uruchomionego kontenera;
2. odpowiedzi `/api/status`, `/api/settings` i pozostałych endpointów;
3. aktualny schemat oraz dane MariaDB;
4. bieżący kod w `/addons/ems_gpt_core` i lokalnym katalogu źródłowym;
5. niniejszy dokument;
6. historyczny changelog V3.

Każdą rozbieżność należy opisać przed jej naprawą. Nie usuwać danych ani tabel w ramach diagnostyki.
