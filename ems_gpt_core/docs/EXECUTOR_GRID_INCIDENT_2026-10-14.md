## Uzupełnienie 2026-10-14 — konflikt Charging=Grid w wykonawcy

Status: przyczyna potwierdzona w kodzie 0.40.28 oraz śladach skryptów HA; poprawka przygotowana na gałęzi `codex/executor-tou-grid-conflict-20261014`, bez wdrożenia do dodatku. Logika i konfiguracja planera nie zostały zmienione.

### Dowody

- Slot 13:00: `BUY_ALLOWED`, plan zakupu 1,239796 kWh, target wykonawcy `soc_charge_target_pct=32,4%`, target TOU zaokrąglony do 32%. Komenda ON została wysłana o 13:00:20,104.
- Skrypt importu ON wystartował o 13:00:20,102, skrypt eksportu OFF o 13:00:20,145. Analogiczna kolejność występuje o 12:30 i 12:45.
- Skrypt importu ON ustawia tylko `Zero Export To Load` i globalne `Battery Grid Charging=ON`. `Charging=Grid` ustawia wcześniej kod Core.
- Następująca po imporcie obsługa `SELL_BAT OFF` w `dispatch_ready_commands()` bezwarunkowo wywołuje `set_active_program_charging(now, "Disabled")`. W ten sposób cofa ustawione przez import `Grid`, mimo pozostawienia globalnego przełącznika ON.
- Skrypty są uruchamiane przez asynchroniczne `script.turn_on`, co dodatkowo dopuszcza nakładanie ich sekwencji.
- Recorder HA nie zwrócił historii selektora programu 3 dla badanego przedziału; nie traktujemy pustej historii jako dowodu braku zmian.
- Po ręcznej zmianie użytkownika program 3 ma `Grid` od 13:01:12. Około 13:09 bateria ładuje się mocą 5,043 kW (odczyt -5043 W według konwencji Deye), SOC 29%, pobór z sieci 6,246 kW. Potwierdza to fizyczne wznowienie ładowania po ustawieniu źródła.

### Przygotowana poprawka

1. Komendy OFF wykonywane przed ON, z deterministycznym porządkiem pobrania komend.
2. Wywołanie nazwanej usługi `script.<nazwa>` zamiast asynchronicznego `script.turn_on`, aby poczekać na zakończenie sekwencji.
3. `SELL_BAT OFF` nie ustawia `Disabled`, jeśli globalne ładowanie z sieci jest ON. Ochrona dotyczy również późniejszego cyklu bez nowej komendy importu ON.
4. Import OFF nadal wyłącza źródło ładowania aktywnego programu. Nieudany skrypt OFF lub błąd wyłączenia źródła blokuje nowe bateryjne ON w tym przebiegu.
5. 22 testy wykonawcy przechodzą, w tym cztery nowe regresje: BUY ON + SELL OFF, późniejszy SELL OFF podczas BUY, poprawny import OFF i blokada ON po błędzie OFF. Kompilacja zmienionych plików przechodzi.

Wymagane sprawdzenie po wdrożeniu: co najmniej jedna automatyczna granica slotu BUY, przy utrzymaniu `Grid` dla aktywnego programu i ładowaniu do targetu. Ręczne ustawienie `Grid` nie jest dowodem skuteczności niewdrożonej poprawki.
