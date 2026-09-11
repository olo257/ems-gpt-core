# EMS-GPT Core 0.24.2 — audyt migracji i stan odbioru

Data audytu: 2026-09-10

## Wynik

Wersja 0.24.2 została uruchomiona produkcyjnie. Proces aplikacji raportuje 0.24.2, odpowiada na healthcheck, utrzymuje połączenie z MariaDB i wejściem Home Assistant. Executor pozostaje wyłączony i nie utworzono żadnych komend urządzeń.

Migracja logiki V3 do Core jest kompletna w zakresie planowania, PPD, telemetrii, wykonania, recovery SLOT/HOUR/DAILY, analityki, procesów i panelu obserwacyjnego. Usunięcie V1/V2/V3 oraz ich tabel SQL pozostaje osobnym, odwracalnym etapem porządkowym i nie powinno nastąpić przed odbiorem świeżego planu 0.24.x.

## Zakres potwierdzony

| Moduł | Stan | Dowód odbiorowy |
|---|---|---|
| Runtime | OK | log startowy `EMS-GPT Core 0.24.2 started`; cykliczne `/health` HTTP 200 |
| MariaDB i wejście HA | OK | status aplikacji: `CONNECTED` |
| PPD | OK | trwała, walidowana macierz BUY/NO_BUY/NEUTRAL, sprzedaż BAT/PV i HP PREFERRED/NEUTRAL/AVOID |
| SOC | OK | niezależne `SOC przed`, `SOC po`, floor i target w slotach i panelu |
| Recovery po awarii | OK | kontrakt `CORE_RECOVERY_0_24_2_R6`; terminalne RECOVERED/MISSING_OUTAGE; HOUR/DAILY bez imputacji actual |
| Alokator PV SLOT | OK strukturalnie | PV→BAT, PV→CWU, PV→EV, PV→sieć i ograniczenie PV są utrwalane |
| Alokator PV HOUR/DAILY | OK strukturalnie | nowe kolumny, sumowanie ze SLOT, API i kolumny panelu |
| Procesy | OK | sześć procesów, override, wykonanie, energia i relacja `slot_id`; próg techniczny 0,050 kWh/slot |
| Executor | bezpiecznie wyłączony | `executor_enabled=false`, dry-run, zero komend |
| Testy offline | OK | kompilacja Python i 19/19 testów kontraktowych |

## Odbiór oczekujący na dane naturalne

1. Po publikacji kompletnej doby RCE wygenerować świeży plan 96-slotowy kodem 0.24.x.
2. Potwierdzić niezerowe wartości alokatora PV w SLOT oraz zgodność ich sum w HOUR i DAILY.
3. Potwierdzić ciągłość `SOC przed` → `SOC po` między kolejnymi slotami.
4. Obserwować co najmniej jedną pełną dobę i zatwierdzić zamknięcie DAILY oraz jakość 92/96/100 slotów zależnie od DST.

Aktualne zera w nowych polach agregatów nie dowodzą błędu 0.24.2: widoczny plan został opublikowany przez starszy silnik, zanim ilościowy alokator zaczął wypełniać te pola. Nie należy wymuszać planu dla następnej doby przed dostępnością kompletnego RCE.

## Otwarte pozycje

- `source_version` w rekordach HOUR nadal przyjmuje historyczną stałą `CORE_0_22_1`. Obliczenia są wykonywane przez 0.24.2, lecz metadane należy zmienić na bieżącą wersję aplikacji w małym wydaniu naprawczym.
- Supervisor może pokazywać buforowaną wersję sklepu `0.24.0`; wersją rozstrzygającą jest runtime i jego log startowy, które potwierdzają 0.24.2. Przy kolejnym wydaniu należy odświeżyć metadane lokalnego repozytorium.
- AI Observer ma jawny status `DISABLED/NOT_IMPLEMENTED`; kontrakt danych istnieje, ale moduł nie został uruchomiony.
- Aktywacja executora wymaga osobnej bramy bezpieczeństwa i odbioru dry-run. Do tego czasu nie wolno usuwać zabezpieczenia fail-closed.
- Fizyczne usunięcie automatyzacji, skryptów i tabel SQL V1/V2/V3 wymaga końcowego spisu zależności, backupu oraz osobnej zgody na destrukcyjne usunięcie.

## Warunek uznania migracji za zamkniętą

Migrację można zamknąć po: odbiorze świeżego planu 96-slotowego, pełnej dobie HOUR/DAILY, korekcie metadanych `source_version`, potwierdzeniu braku konsumentów V1/V2/V3 oraz decyzji dotyczącej AI Observer i executora.
