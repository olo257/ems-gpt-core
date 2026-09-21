# EMS-GPT Core — historia zmian

Ten plik rejestruje wydania. Nie jest specyfikacją; obowiązujące reguły są w `DOCS.md` i `PLANNER_CONTRACT.md`.

## 0.38.8 — bezpieczna migracja relacji `slot_id`

- Zastąpiono pełne, restartowe `UPDATE JOIN` migracją wykonywaną partiami dobowymi.
- Przed backfillem tworzone są indeksy dla `slot_id/slot_start` oraz kalendarza lokalnych slotów.
- Po każdej partii wykonywany jest commit; błąd powoduje rollback bieżącej partii, a kolejny start bezpiecznie wznawia migrację.
- Po pełnym zakończeniu zapisywany jest trwały marker `slot_id_backfill_batched_0_38_8`; kolejne restarty nie powtarzają aktualizacji historycznej.
- Połączenie używane przez migrację ma lokalnie wydłużony timeout 120 s, bez zmiany zwykłych połączeń runtime.
- Dodano test regresyjny symulujący 730 dni historii i 5 110 krótkich partii.

## 0.38.7

- Naprawiono ostatni etap ochrony `BATTERY_IMPORT`: `planned_buy_kwh` pobrane z
  MariaDB jest teraz konwertowane jako skalar SQL, a nie jako obiekt stanu Home
  Assistant. Poprawny zakup nie jest już odrzucany z
  `IMPORT_PLAN_OR_SOC_UNAVAILABLE` mimo obecnej energii planu, targetu i SOC.

## 0.38.6

- Executor wiąże ochronę `BATTERY_IMPORT` z dokładnym `plan_run_id` zapisanym
  w wersji komendy. Nie może już odrzucić poprawnego zakupu jako
  `IMPORT_PLAN_OR_SOC_UNAVAILABLE` wskutek odczytu innego rekordu tego samego
  slotu z `planned_buy_kwh=NULL`.
- Obserwacja `PV_CWU` korzysta ze stanu rzeczywiście sterowanej grzałki
  `light.sm_pro_3248d_1_grzalka_cwu`. Autonomiczne grzanie CWU przez pompę
  ciepła nie jest już klasyfikowane jako ręczne uruchomienie `PV_CWU`.
- Obserwacja `BATTERY_EXPORT` obejmuje wyłącznie rozładowanie przypisane do
  rzeczywistego eksportu sieciowego. Autokonsumpcja bateria→dom nie jest już
  klasyfikowana jako ręczna sprzedaż.

## 0.38.5

- Naprawiono klasyfikację wykonania `BATTERY_IMPORT`: ładowanie baterii z PV
  nie jest już oznaczane jako ręczne uruchomienie importu. Obserwowana energia
  procesu obejmuje wyłącznie część dodatniego importu sieciowego pozostałą po
  pokryciu rzeczywistego deficytu odbiorów i jest ograniczona zmierzonym