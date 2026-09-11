# EMS-GPT Core 0.21.0 — kompletny pakiet offline do odbioru etapowego

**Data:** 2026-09-09  
**Baza:** produkcyjny pakiet 0.20.0  
**Stan:** przygotowany i zwalidowany statycznie; niewdrożony; brak walidacji na rzeczywistym HA/MariaDB  
**Executor:** `false`, `DRY_RUN=true`

## Wynik przygotowania

Pakiet łączy brakujące elementy kolejki P1–P5 w jednym źródle, ale rozdziela ich uruchamianie bramkami opisanymi w `VALIDATION_STAGES.md`. Zwykła instalacja 0.21.0 nie uruchamia sterowania urządzeniami.

### P1 — niezależność źródeł

- dodano bezpośredni `sensor.inverter_battery_power`;
- domyślnie nadal używane są helpery V3;
- przełączenie wymaga jawnego wyboru znaku `charge_positive` lub `discharge_positive` po pomiarach rzeczywistych;
- bootstrap z bazy źródłowej jest domyślnie wyłączony także w kodzie, nie tylko w opcjach dodatku.

### P2 — PPD i SOC

- zachowano jeden przebieg wsteczny O(n);
- dodano bazowy cel SOC 60% o 20:00;
- dodano wagę wartości końcowego SOC;
- dodano wagę niepewności prognozy;
- zachowano zakazy zakupu w oknach sprzedażowych i sprzedaży PV przy cenie ≤0;
- brak poboru EV przy otwartym oknie nie jest automatycznie uznawany za błąd.

### P3 — operator i procesy

- trwałe `AUTO`, `FORCE_ON`, `FORCE_OFF` dla sześciu procesów;
- czas ważności, użytkownik, przyczyna, anulowanie i wygaśnięcie;
- responsywne karty operatora w widoku Procesy;
- ręczna cyrkulacja domyślnie 45 minut;
- trwała projekcja plan → stan efektywny → stan obserwowany → energia.

### P4 — analityka, diagnostyka, TODO i AI

- tabela wykonania procesów per slot;
- diagnostyka komend wygasłych, override i bramy executora;
- automatyczne wpisy TODO z alertów diagnostycznych;
- trwały kontrakt przebiegów AI pozostający domyślnie wyłączony;
- API do historii procesów, TODO i przebiegów AI.

### P5 — konektor

- trwałe komendy z `command_id`, slotem, procesem, decyzją, TTL i wersją planu;
- idempotencja intencji przez unikalny klucz;
- stany `DRY_RUN`, `READY_FOR_CONNECTOR`, `DISPATCHED`, `ACCEPTED`, `EXECUTED`, `REJECTED`, `FAILED`, `EXPIRED`;
- potwierdzenie konektora i blokada przyjmowania wygasłych komend;
- fail-closed allowlista wyłącznie encji `script.*`;
- jawna blokada mapowań odnoszących się do programów SOC;
- potrójna brama aktywacji: enabled, wyłączony dry-run i dokładne potwierdzenie techniczne.

## Testy wykonane offline

- `python3 -m py_compile app.py`: PASS;
- parser YAML i kontrola bezpiecznych wartości startowych: PASS;
- 8 testów kontraktowych: PASS;
- kontrola braku odwołań zapisujących programy SOC 1–6: PASS.

## Czego nie uznano za zweryfikowane

- pierwszy kompletny slot pól 0.20.0/0.21.0;
- jednostki oraz COP na danych rzeczywistych;
- znak bezpośredniej mocy baterii;
- migracje na istniejącej bazie produkcyjnej;
- wyniki nowego PPD dla rzeczywistych 96 rekordów;
- zachowanie UI przez Ingress;
- lokalne blokady sześciu skryptów HA;
- jakiekolwiek sterowanie urządzeniami.

## Pierwszy krok po powrocie do weryfikacji

Rozpocząć od G0 i G1. Nie zmieniać `direct_battery_power_mode`, nie włączać executora i nie wpisywać mapy usług przed odebraniem telemetrii oraz migracji. Każdą bramkę kończyć wpisem do changelogu; rollback dla G0 to ponowne uruchomienie pakietu 0.20.0 z zachowaniem `/data` i bazy.

