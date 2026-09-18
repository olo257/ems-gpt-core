# EMS-GPT Core 0.21.0 — odbiór etapowy

Pakiet został przygotowany offline. Nie wolno przejść do następnej bramki, jeżeli poprzednia nie została zamknięta wynikiem PASS i wpisem w changelogu.

## G0 — instalacja bez funkcji wykonawczych

- zachować rzeczywiste hasło MariaDB w opcjach Supervisor;
- `executor_enabled=false`;
- `executor_dry_run=true`;
- `direct_battery_power_mode=legacy_helpers`;
- sprawdzić kompilację, migracje, `/health`, `/api/status` i wszystkie endpointy GET;
- potwierdzić brak wywołań usług urządzeń oraz brak zmian V3 i SOC 1–6.

## G1 — odbiór telemetrii 0.20/0.21

- co najmniej jeden naturalnie zamknięty slot;
- SOC start/min/end/delta;
- temperatura `sensor.klimat_w_ogrodzie_temperature`;
- PV1/PV2/razem;
- energia heat/DHW/cool i COP z poprawnym `NULL` przy zerowej energii wejściowej;
- tryb HP, licznik uruchomień i godziny pracy;
- brak błędów JS/API w Wykonaniu.

## G2 — bezpośrednia moc baterii

1. Pozostawić `legacy_helpers` i zebrać równolegle wartości surowe.
2. Potwierdzić znak w ładowaniu, spoczynku i rozładowaniu.
3. Wybrać `charge_positive` albo `discharge_positive`.
4. Porównać minimum cztery naturalne sloty z helperami.
5. Sprawdzić bilans i brak jednoczesnej energii ładowania/rozładowania.

Rollback: `direct_battery_power_mode=legacy_helpers`.

## G3 — PPD

- porównać stary i nowy plan na identycznych 96 wejściach;
- sprawdzić wartość terminalną SOC i cel około 60% o 20:00;
- sprawdzić SOC 90–100% z dalszą produkcją PV;
- potwierdzić brak zakupu w oknie sprzedaży i brak sprzedaży PV przy cenie ≤0;
- brak dziennego limitu EV; odłączenie auta nie obniża jakości planu;
- przebieg pozostaje O(n), dokładnie 96 slotów.

## G4 — procesy operatorskie

- osobno każdy z sześciu procesów;
- `AUTO`, `FORCE_ON`, `FORCE_OFF`, wygaśnięcie i anulowanie;
- ręczna cyrkulacja 45 minut;
- historia przyczyny, użytkownika i ważności;
- zapis plan vs stan efektywny vs obserwacja;
- brak poleceń do HA.

## G5 — kontrakt konektora w DRY_RUN

- `executor_enabled=true`, ale `executor_dry_run=true`;
- jedna intencja komendy na slot/proces/plan/decyzję;
- poprawne TTL, `command_id`, `plan_version` i źródło override;
- idempotentne potwierdzenia;
- odrzucenie potwierdzenia wygasłej komendy;
- recovery nie ponawia wygasłych poleceń.

Rollback: `executor_enabled=false`.

## G6 — konektor produkcyjny

Nie jest dopuszczony samym zainstalowaniem 0.21.0. Wbudowany adapter działa fail-closed i wywołuje wyłącznie jawnie zmapowane encje `script.*`. Aktywacja wymaga jednocześnie `executor_enabled=true`, `executor_dry_run=false` oraz technicznego potwierdzenia `executor_activation_ack=EMS_CONNECTOR_ACCEPTED`. Najpierw trzeba zweryfikować lokalne blokady w każdym skrypcie HA, uzupełnić mapę usług i przetestować potwierdzenia.

Procesy uruchamiać pojedynczo: PV→CWU, PV→EV, HP_DHW, import baterii, pozostałe procesy, sprzedaż baterii na końcu. Programy SOC 1–6 pozostają bez prawa zapisu. Pusta lub niepoprawna mapa usług odrzuca polecenie.

## G7 — pełna doba i awarie

- 96 slotów bez luk, duplikatów i skoków;
- restart EMS-GPT Core w różnych fazach slotu;
- osobne testy restartu HA i hosta;
- utrata MariaDB, HA API oraz konektora;
- kontrola północy, RCE około 14:00 i strefy `Europe/Warsaw`;
- hourly/daily zgodne z SLOT;
- diagnostyka oraz TODO bez duplikowania alertów.
