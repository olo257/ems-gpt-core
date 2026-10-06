# EMS-GPT AI Observer Worker — historia zmian

## 0.1.8 — izolacja awarii i diagnostyka LLM

- Błąd odpowiedzi na pytanie nie pomija przeglądu okresowego; oba przebiegi
  mają niezależne ponowienia z opóźnieniem 60–900 sekund.
- Log wskazuje przebieg, kod HTTP i znany kod dostawcy, np. brak limitu API
  lub niepoprawny klucz. Nie zapisuje treści odpowiedzi błędu ani sekretów.
- Nieudany przegląd nie przesuwa znacznika ukończonej analizy ani historii.
- Pytania bez odpowiedzi nadal mogą zostać przejęte po wygaśnięciu
  istniejącej dziesięciominutowej dzierżawy Core.
- Zmiana nie modyfikuje Core, planera, SOC, PPD ani egzekutora.
- Naprawa nie zastępuje korekty klucza lub limitu API, jeżeli diagnostyka
  wskaże problem po stronie dostawcy modelu.
