# EMS-GPT Core 0.25.12

- Ręczne `FORCE_ON` procesu `HP_HEAT_DHW` przyjmuje w panelu czas od 2 do 24 godzin, z krokiem 15 minut.
- Backend egzekwuje minimalny czas równy skonfigurowanej minimalnej długości cyklu HP.
- Zewnętrznie uruchomiona sprężarka nie jest wyłączana przez plan `OFF`; wykonawca zachowuje sterowanie zewnętrzne.
- Jawna ręczna blokada `FORCE_OFF` pozostaje nadrzędna wobec sterowania zewnętrznego.
- Blokada `HP_HEAT_DHW` jest bezterminowa aż do ręcznego powrotu do `Auto`; panel pokazuje ją na czerwono.
- Zewnętrzne grzanie jest zapisywane jako efektywny stan `ON` / `EXTERNAL_MANUAL` i wliczane do dziennego minimum ogrzewania.
- Detekcja zewnętrznej pracy opiera się na świeżej częstotliwości sprężarki; pobór czuwania nie jest uznawany za pracę HP.

Kolejność: `MANUAL_BLOCK` > dowolne aktywne wymuszenie ON (`EXTERNAL_MANUAL` lub `MANUAL_FORCE_ON`) > `AUTO`.
