# EMS-GPT Core 0.24.3 — odbiór poprawki

Data: 2026-09-10

## Zakres

- zastąpiono historyczną stałą `source_version=CORE_0_22_1` wersją wyliczaną z `APP_VERSION`;
- ujednolicono wersję runtime, manifestu i metadanych Supervisor;
- nie zmieniono zasad planera, PPD, recovery ani sterowania.

## Wynik produkcyjny

- Supervisor: wersja zainstalowana i najnowsza `0.24.3`, brak dostępnej aktualizacji;
- runtime `/health`: HTTP 200, wersja `0.24.3`;
- status: `RUNNING`, MariaDB i wejście HA `CONNECTED`, brak ostatniego błędu;
- świeże agregaty HOUR: `source_version=CORE_0_24_3`;
- executor: `CONNECTOR_REQUIRED`, konfiguracja wykonawcza wyłączona;
- komendy: 0;
- tymczasowe `init_commands`: usunięte;
- rollback źródeł 0.24.2: `/share/ems-gpt-core-rollback-0.24.2`;
- walidacja offline: kompilacja Python i 20/20 testów PASS.

## Nadal obowiązujące bramy

Semantyczny odbiór ilościowego alokatora PV wymaga świeżego planu utworzonego po publikacji kompletnego RCE. Fizyczne usunięcie V1/V2/V3 i ich tabel SQL pozostaje zablokowane do końcowego spisu zależności, pełnej doby odbiorowej i osobnej zgody na operację destrukcyjną.
