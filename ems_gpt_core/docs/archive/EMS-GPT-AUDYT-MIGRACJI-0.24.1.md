# EMS-GPT Core 0.24.1 — audyt migracji modułów

Data: 2026-09-10

## Wynik

Migracja rdzenia planistycznego i obserwacyjnego z V3 jest funkcjonalna, ale nie jest jeszcze podstawą do usunięcia V1/V2/V3. Wersja 0.24.1 domyka wykryte niespójności relacji slotów, diagnostyki i klasyfikacji przepływów technicznych. Executor pozostaje wyłączony.

| Moduł | Stan | Ustalenie |
| --- | --- | --- |
| CORE / scheduler | gotowy | Pętla minutowa, watchdog i restartowe odzyskanie działają. |
| Telemetria | gotowa obserwacyjnie | PV1/PV2, sieć, bateria, EV, CWU i HP są próbkowane; awaria pozostawia jawne braki. |
| RCE | gotowy | Import dzisiejszych/jutrzejszych cen i brama 96 rekordów działają fail-closed. |
| Prognozy PV/pogody | gotowe | Open-Meteo i profile lokalne działają bez ciągłego zapisu V3. |
| Profil zużycia | gotowy z bramą jakości | Niepełne sloty nie powinny zasilać uczenia. |
| Planer | gotowy obserwacyjnie | Transakcyjny staging i publikacja, 96 slotów po kompletnym RCE. |
| SOC | gotowy obserwacyjnie | Rozdzielone SOC przed/po/floor/target i ciągłość trajektorii. |
| PPD | gotowy obserwacyjnie | Jawna macierz sieci, eksportu i HP; sześć procesów. |
| Ilościowy allocator PV | częściowy | Działa per SLOT; jego osobne przepływy nie są jeszcze sumowane w HOUR/DAILY. |
| Wykonanie slotu | gotowe | Aktualne energie, SOC i HP; techniczne przepływy baterii <0,050 kWh nie są procesem EMS. |
| Relacje slot_id | gotowe od 0.24.1 | Nowe przebiegi procesów zapisują slot_id w chwili INSERT; historia podlega backfillowi. |
| Recovery HOUR/DAILY | gotowe | Sloty bez danych kończą się jako MISSING_OUTAGE; agregaty są odbudowywane bez imputacji actual. |
| Analityka | gotowa podstawowo | WAPE PV1/PV2/PV/load/import/export; potrzebna dalsza walidacja naturalnych dób. |
| Diagnostyka | gotowa podstawowo | Od 0.24.1 horyzont cen przed 14:00 jest oceniany czasowo poprawnie. |
| TODO | gotowe | Otwarte alarmy deduplikowane per dzień/moduł/tytuł. |
| AI Observer | brak migracji | Istnieje tabela/API, ale brak wykonawcy i harmonogramu; status jawnie NOT_IMPLEMENTED. |
| Executor | kontrakt gotowy, aktywacja nieodebrana | TTL, idempotencja, allowlista i ACK istnieją; wykonanie pozostaje wyłączone. |
| Panel | gotowy podstawowo | Widoki i konfiguracja kolumn działają; HOUR/DAILY wymagają pól allocatora. |

## Bramy przed usunięciem V1/V2/V3

1. Dodać przepływy allocatora PV do HOUR i DAILY wraz z migracją SQL oraz panelem.
2. Zdecydować, czy wdrażamy AI Observer, czy formalnie usuwamy go z zakresu produkcyjnego.
3. Odebrać minimum jedną pełną dobę 0.24.1: 96/96 terminalnych slotów bez nieuzasadnionych braków.
4. Zweryfikować PPD i SOC na nowym planie opublikowanym po pełnym imporcie RCE.
5. Odbierać executor procesami; sprzedaż baterii jako ostatnia. Do tego czasu brak równoległego sterowania.
6. Dopiero po odbiorze executora zinwentaryzować i usuwać automatyzacje, skrypty, helpery oraz tabele legacy.

## Bezpieczeństwo

- `executor_enabled=false`, `executor_dry_run=true`.
- Brak poleceń do urządzeń i brak zapisów programów SOC 1–6.
- Zamknięte rekordy historyczne nie są przepisywane wartościami szacowanymi.
- Usuwanie V1/V2/V3 i ich tabel SQL pozostaje zablokowane do przejścia powyższych bramek.
