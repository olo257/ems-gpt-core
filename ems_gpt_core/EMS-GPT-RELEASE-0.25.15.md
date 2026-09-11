# EMS-GPT Core 0.25.15

- Naprawia `UnboundLocalError` podczas zamykania slotu: obserwowany stan HP jest wyliczany przed klasyfikacją `EXTERNAL_MANUAL`.
- Dodaje test regresyjny kolejności obliczeń dla zewnętrznego sterowania HP.
- Zachowuje funkcje 0.25.14 oraz wykonawcę bezpiecznie `OFF` przy pustym mapowaniu konektora.
