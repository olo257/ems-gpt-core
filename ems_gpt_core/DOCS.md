# EMS-GPT Core

## Dostosowanie tabel

W panelu dodatku otwórz kartę **Konfiguracja**, a następnie sekcję **Kolumny tabel**.
Wybierz tabelę z listy i zaznacz kolumny, które mają być widoczne. Zmiana jest
zapisywana automatycznie w pamięci lokalnej przeglądarki. Przycisk
**Przywróć domyślne** odtwarza pełny zestaw obserwacyjny danego widoku.

Ustawienie kolumn jest osobne dla każdej przeglądarki i urządzenia. Nie zmienia
danych w MariaDB ani algorytmu planowania.

Niezależny silnik EMS uruchamiany jako lokalna aplikacja Home Assistant.

## Odpowiedzialność

- CORE: zegar 15-minutowy, telemetria, wznowienie i zamykanie slotów.
- PLANER: transakcyjny przebieg RCE → FORECAST → WINDOWS → SOC → PPD → VALIDATE.
- PPD: polityki sieci, eksportu oraz dostępność PV→CWU i PV→EV.
- ANALYTICS: agregaty godzinowe i dobowe oraz profil zużycia.
- EXECUTOR: celowo nieaktywny do czasu wdrożenia konektora wykonawczego.

## Dane

Aplikacja używa osobnej bazy MariaDB `ems_gpt` i konta `ems_gpt_app`.
Nie ma dostępu do bazy rekordera Home Assistant. Migracja początkowa 50 tabel
`ems_gpt_*` została wykonana jednokrotnie, a bootstrap pozostaje wyłączony.

## Bezpieczeństwo

- aplikacja nie wywołuje usług urządzeń;
- aktywny i zamknięte sloty nie są nadpisywane przez replan;
- publikacja planu następuje atomowo dopiero po pełnej walidacji;
- brak cen RCE nie jest zastępowany zerem ani stałą ceną;
- watchdog kontroluje połączenie z MariaDB i pracę procesu.

## Panel

Panel Ingress zawiera status modułów oraz widoki Planer, Wykonanie,
Godzinowe i Dobowe.

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

Bez potwierdzonego trybu znaku `sensor.inverter_battery_power` należy pozostawić
Domyślnie `direct_battery_power_mode=discharge_positive`, zgodnie ze znakiem mocy Deye
(wartość dodatnia oznacza rozładowanie, ujemna ładowanie). Zachowana wartość
`legacy_helpers` jest automatycznie migrowana do tego trybu i nie odczytuje helperów V1/V2/V3.

RCE bieżącego slotu jest pobierane bezpośrednio z `ems_gpt_slots`; aplikacja nie wymaga
sensora `sensor.ems_gpt_rce_pse_current`. Po awarii aplikacja domyślnie odbudowuje
SLOT/HOUR/DAILY z ostatnich 7 dni. Zakres można zmienić w karcie Konfiguracja parametrem
„Zakres odtwarzania po awarii [dni]”.
