# EMS-GPT Core 0.25.17

## Krytyczna poprawka planowania HP

- Zużycie energii przez zaplanowane okno `HP_HEAT_DHW` jest doliczane do obciążenia każdego slotu przed wyliczeniem SOC.
- Zapotrzebowanie HP wpływa teraz na `soc_floor`, `soc_target`, rozładowanie baterii oraz wcześniejszy zakup w oknie `BUY_ALLOWED`.
- Dzięki temu planer uwzględnia ryzyko nocnego spadku SOC i konieczności późniejszego drogiego importu.
- Zachowano minimum 10 godzin grzania, cykl minimum 2 godziny oraz przerwy 1–3 godziny.
- Ślad decyzji zapisuje planowaną energię HP dla slotu bez dodawania zbędnej kolumny do panelu.
