# EMS-GPT Core 0.25.0 — pakiet aktualizacji ręcznej

Data przygotowania: 2026-09-10

## Zakres jednego pakietu

- komplet migracji analiz V3: WAPE PV1/PV2/PV razem, zużycia, importu i eksportu;
- bias PV, zużycia, importu i eksportu;
- średni bezwzględny błąd SOC oraz odchylenie wyniku PLN;
- profile zużycia i produkcji PV wyłącznie z danych spełniających bramę jakości;
- AI Observer `SHADOW_READ_ONLY` z trwałym promptem wejściowym, wynikiem, autooceną, decyzją i sugestiami/TODO;
- osobna karta panelu AI Observer oraz rozszerzona karta Analityka;
- planer wykorzystujący wszystkie ciągłe przyszłe sloty z dostępnym RCE, bez limitu 96;
- diagnostyka pełnego horyzontu RCE z obsługą dób 92/96/100 slotów.

## Granice bezpieczeństwa

AI Observer nie ma ścieżki zapisu do planu, PPD, komend ani usług Home Assistant. Nie wykonuje zewnętrznego wywołania modelu i jawnie zapisuje `external_model_called=false`. Executor pozostaje wyłączony, a programy SOC 1–6 są chronione.

## Instalacja

Aktualizacja została przygotowana do ręcznego uruchomienia z poziomu Supervisor. Automatyczna instalacja, przebudowa i restart dodatku nie są wykonywane przez proces przygotowania.

Po ręcznej aktualizacji należy potwierdzić:

1. wersję runtime 0.25.0 i healthcheck;
2. migrację schematu `core_schema_0_25_0`;
3. pierwszy ręczny przebieg Analityki i AI Observer;
4. status Observera `SHADOW_READ_ONLY`;
5. brak komend executora;
6. liczbę slotów planera większą niż 96, jeżeli w bazie dostępny jest dłuższy ciągły horyzont RCE.

Uwaga: istniejąca instalacja może zachować poprzednią opcję `ai_observer_enabled=false`. Po ręcznym wdrożeniu należy ją włączyć w opcjach dodatku albo zlecić jej włączenie osobno; dopiero wtedy Observer wykona przebieg.
