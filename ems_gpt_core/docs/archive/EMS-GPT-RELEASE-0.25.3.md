# EMS-GPT Core 0.25.3 — ręczna aktualizacja

## Zakres

- siódmy proces `HP_HEAT_DHW` w planerze, API, override, przebiegach i panelu;
- osobna karta `Włącz / Blokuj / Auto` dla `HP_HEAT_DHW`;
- `HP_HEAT_DHW` działa w oknach HP tylko przy nocnym minimum temperatury poniżej progu z konfiguracji;
- brak prognozy nocnej blokuje automatyczne `Heat+DHW`;
- poprawka serializacji dat w historii zdarzeń override;
- marża zakupu pozostaje pobierana dynamicznie z konfiguracji aplikacji.

## Skrypty Home Assistant

W HA przygotowano 14 idempotentnych skryptów CORE: osobne `ON` i `OFF` dla
`BATTERY_IMPORT`, `BATTERY_EXPORT`, `PV_CWU`, `PV_EV`, `MANUAL_CIRCULATION`,
`HP_DHW` oraz `HP_HEAT_DHW`.

`HP_HEAT_DHW ON` ustawia tryb `Heat+DHW` i włącza główne zasilanie pompy.
`HP_HEAT_DHW OFF` przywraca `DHW only`, nie wyłączając pompy ciepła.

## Bezpieczne uruchomienie

Po ręcznej aktualizacji sprawdzić kolejno:

1. wersję `0.25.3`, stan MariaDB i wejścia HA;
2. obecność siedmiu kart w widoku Procesy;
3. `executor_enabled=true`, `executor_dry_run=true`;
4. wygenerowanie komend `DRY_RUN` bez wywołań urządzeń;
5. komplet mapy skryptów ON/OFF.

Aktywacja produkcyjna wymaga dopiero potem ustawienia
`executor_dry_run=false` oraz `executor_activation_ack=EMS_CONNECTOR_ACCEPTED`.
Programy SOC 1–6 nie są modyfikowane.
