Warning: truncated output (original token count: 24058)
Total output lines: 1619

# EMS-GPT Core — historia zmian

## 0.39.35 — bezpieczne odzyskiwanie planera przy nieosiągalnym terminie BUY

- Jeśli etap `TARGET_COMMITMENT` nie znajduje wykonalnej trajektorii SOC,
  planer najpierw wyłącza sprzedaż baterii, a następnie ponawia przebieg bez
  wymuszonego terminu realizacji opcjonalnego zakupu.
- Ponowienie zachowuje wymagany SOC w każdym slocie, rezerwę bezpieczeństwa,
  cel końca doby i limit importu. Zakup z sieci nie może przekroczyć
  wymaganego SOC; ładowanie z PV nadal może przekroczyć ten poziom.
- Jeśli trajektoria pozostaje niewykonalna przy zachowaniu twardych wymagań,
  planer nadal zgłasza błąd i pozostaje w trybie fail-safe.
- Dodano test regresji fallbacku. Weryfikacja lokalna: 325 testów zaliczonych.

## 0.39.34 — odzyskiwanie z nieosiągalnego historycznego SOC końca horyzontu

- Przy błędzie `No feasible terminal SOC state for complete horizon` planner
  najpierw wyłącza sprzedaż baterii, a następnie może obniżyć wyłącznie
  historyczny cel końcowy do niezależnego SOC bezpieczeństwa.
- Dotyczy także horyzontu kończącego się w przyszłej dobie. Wymagania mostu
  SOC i minimalna rezerwa pozostają twarde; jeżeli sam poziom bezpieczeństwa
  jest nieosiągalny, przebieg nadal kończy się błędem i nie publikuje planu.
- Dodano testy kolejności fallbacku: najpierw wyłączenie sprzedaży, potem
  relaksacja celu historycznego, bez relaksacji bezpieczeństwa.
- Weryfikacja lokalna: 324 testy zaliczone; kompilacja modułów Python poprawna.

## 0.39.33 — wspólna osiągalność SOC i dispatchu bez sprzedaży baterii

- Walidator mostu SOC liczy osiągalność według tych samych zasad co retry
  planera z wyłączoną sprzedażą baterii.
- BUY nie może podtrzymywać SOC równolegle z dobrowolnym importem dla domu;
  wymagania przekraczające wspólnie osiągalną trajektorię są ograniczane do
  fizycznie wykonalnej ścieżki, a sprzedaż baterii jest wyłączana.
- Dodano regresje dla importu domu ukrywanego w BUY oraz dla trajektorii,
  której osiągalność lokalna nie zgadzała się z pełnym przebiegiem.
- Weryfikacja: 23 testy bezpieczeństwa SOC zaliczone.

## 0.39.32 — PPD nadwyżki PV dopiero przy pełnym SOC

- `soc_target` nadal ogranicza wyłącznie ładowanie baterii z sieci; ładowanie
  baterii z PV może trwać do fizycznego 100% SOC.
- Kandydackie okno `PV_CWU`/`PV_EV` jest otwierane dopiero dla slotów z
  prognozowanym SOC 100%; świeży pomiar wykonawczy poniżej 100% blokuje oba
  procesy, nawet gdy przekroczył `soc_target`.
- Zaktualizowano opis produkcyjny i kontrakt planera, usuwając sprzeczne
  reguły, które dopuszczały odbiorniki elastyczne po samym osiągnięciu targetu.
- Dodano regresje dla prognozowanego i rzeczywistego SOC poniżej 100%.
- Weryfikacja: 38 testów PPD/executora zaliczonych. Nie oznacza to instalacji
  ani odbioru wydania w Home Assistant.

## 0.39.31 — spójny target zakupu i walidacja całego przepływu

- Target wybranego BUY jest wyprowadzany z wymagania po oknie uzupełnienia; nie jest kopiowany z SOC znalezionego przez nieograniczony przebieg ekonomiczny. Ujemna cena nie powoduje sama zakupu do 100%.
- Wcześniejsze sloty tego samego mostu dziedziczą target BUY, aby dostępne PV mogło zastąpić późniejszy import.
- Resztkowe PV poniżej kwantu SOC respektuje pozostałą moc ładowania i fizyczną pojemność.
- Recovery i optymalizator nie traktują nakładających się BUY/SELL jako uprawnionego zakupu.
- PPD/executor wymagają zgodności `eligible` z jawną polityką `ALLOW/BLOCK` lub `ALLOWED/BLOCKED`.
- Poprawiono sprzeczne zapisy kontraktu dotyczące targetu >100%, limitu PV i recovery.
- Testy regresyjne obejmują zakup przy ujemnej cenie, PV przed BUY, limit mocy, nakładające się okna i niespójne uprawnienia.

## 0.39.28 — granica recovery SOC i alarm diagnostyczny

- Walidacja limitu SOC działa po recovery ograniczonym do slotów przed pierwszym przyszłym BUY; wymagania po BUY nadal odrzucają plan bez obniżania rezerwy.
- Diagnostyka uwzględnia stan runtime planera/PPD i błędy zatrzaśnięte, nawet gdy w bazie pozostaje starszy plan.
- Testy regresyjne obejmują recovery przed BUY, twardy błąd po BUY i raportowanie zdegradowanego planera.
- Kontrola spójności wersji obejmuje dokumentację i oba pliki README.


## 0.39.27 — zgodność wersji API i dodatku

- Ujednolicono wersję zgłaszaną przez API Core z wersją wydania dodatku.
- Status i endpoint `/live` raportują teraz wersję 0.39.27.


## 0.39.26 — odzyskiwanie planu po naruszeniu bufora SOC

- Jeżeli bieżący SOC jest już poniżej bufora bezpieczeństwa, planer utrzymuje
  techniczną rezerwę do pierwszego przyszłego okna BUY zamiast odrzucać cały
  przebieg jako `SOC_SAFETY_BRIDGE_UNREACHABLE`.
- W pierwszym dostępnym oknie BUY przywraca pierwotne wymaganie SOC, aby
  zaplanować możliwe uzupełnienie baterii. Planer zapisuje zakres odzyskiwania
  w audycie.
- Dodano regresję potwierdzającą, że plan z niskiego SOC zachowuje rezerwę i
  uwzględnia zakup w najbliższym oknie BUY.


Ten plik rejestruje wydania. Nie jest specyfikacją; obowiązujące reguły są w `DOCS.md` i `PLANNER_CONTRACT.md`.

## 0.39.25 — widok wykonania slotów i usunięcie Agenta AI

- Widok Wykonanie zestawia plan z wykonaniem dla slotów 15-minutowych:
  PV, zużycie, import, eksport, ładowanie i rozładowanie baterii, PV→EV oraz SOC.
- Bieżący slot jest zwracany jako `IN_PROGRESS` i oznaczony „W toku”.
  Pokazuje plan, a niewypełnione jeszcze pola wykonania pozostają puste.
  Tabela odświeża się w istniejącym cyklu 30 sekund.
- Usunięto z Core skrzynkę Agenta AI, jej API, konfigurację, panel, Worker
  add-on i integrację wyników workera z Observerem. Wbudowany AI Observer
  pozostaje bez zmian w trybie `SHADOW_READ_ONLY`.
- Nie usunięto danych historycznych: tabela wiadomości oraz rekordy
  Observera zachowują dotychczasową zawartość.
- Testy kontraktów potwierdzają pary plan/wykonanie, widoczność bieżącego
  slotu oraz pozostawienie panelu AI Observera.

## 0.39.23 — tryb bezpieczny po błędzie planera lub PPD

- Po błędzie planera, PPD albo oczekiwaniu na poprawny plan wstrzymywane są
  decyzje z poprzedniego przebiegu; executor wystawia OFF dla PV_CWU, PV_EV
  oraz HP_HEAT_DHW.
- Skrypt OFF dla HP przełącza pompę na „DHW only” i pozostawia jej zasilanie.
  Polecenia bezpieczeństwa są wysyłane także przy niedostępnej telemetrii.
- Po poprawnym przebiegu planera i PPD odrzucone są niewysłane polecenia
  awaryjne, a sterowanie wraca do bieżącego planu i istniejących override'ów.
- Zabezpieczenie nie zapisuje celów SOC ani nie zmienia reguł BUY/SELL.

## 0.39.24 — zabezpieczenie PPD i odporność rozruchu

- Przy błędzie planera/PPD lub braku poprawnego planu executor odrzuca stare
  decyzje i wystawia OFF dla PV_CWU, PV_EV oraz HP_HEAT_DHW; skrypt HP ustawia
  „DHW only” i pozostawia pompę zasiloną. Polecenia awaryjne są audytowane.
- Core nie rozpoczyna automatycznego replanu bez świeżej telemetrii. Brak albo
  nieprawidłowa migawka TOU jest traktowana jako niedostępność wejścia; ponowienia
  planera mają ograniczony backoff 30–300 s.
- AI Observer Worker 0.1.7 czeka na endpoint `/live` Core i ponawia połączenie
  co 10 s. Nie publikuje ponownie ustalenia odrzuconego lub rozwiązanego bez
  dowodu z czasu po przeglądzie.
- Zmiany nie modyfikują algorytmu SOC ani reguł BUY/SELL; dodatkowe odbiory
  pozostają wyłączone w ścieżce fail-safe.
- Dodano testy dla poleceń fail-safe, backoffu planera, nieobecnej migawki TOU,
  oczekiwania Worker na Core oraz filtrowania starych findingów.

## 0.39.22 — plan i wykonanie zestawione parami

- Widoki godzinowy i dobowy pokazują główne wielkości obok siebie: PV plan / PV wykonanie, zużycie plan / zużycie wykonanie, a następnie import, eksport, ładowanie i rozładowanie baterii oraz wynik PLN.
- Pozostałe wielkości planowane i wykonane są umieszczone po parach głównych; kolumny jakości i SOC zachowują dotychczasowe dane.
- Nagłówki są jednopoziomowe i zachowują etykiety „plan” oraz „wykon.”, dzięki czemu nie sugerują osobnych bloków planu i wykonania.
- Bieżąca godzina i doba nadal są prezentowane z wartościami narastającymi. Ograniczenie do zamkniętych rekordów pozostaje wyłącznie w widoku 15-minutowym.
- Zmiana dotyczy kolejności i nagłówków kolumn; nie zmienia obliczeń planera ani wykonania.

## 0.39.21 — czytelne zestawienie planu i wykonania oraz odpowiedzi agenta

- Widoki godzinowy i dobowy grupują kolumny w bloki „Planowane”,
  „Wykonanie narastająco” oraz „Jakość i podsumowanie”. Zachowują otwartą
  bieżącą godzinę i bieżącą dobę z dostępnym wykonaniem; filtr wyłącznie
  zamkniętych slotów nadal dotyczy tylko widoku 15-minutowego.
- Widok agenta sprawdza nowe wiadomości co 15 sekund i przebudowuje listę
  tylko wtedy, gdy zmieni się jej treść lub status. Zachowuje pozycję czytania,
  zamiast przewijać do końca przy każdym odświeżeniu.
- Worker domyślnie odbiera nowe pytania z Core co 5 sekund (wcześniej 30 s);
  ustawienie pozostaje konfigurowalne.
- Powiadomienia e-mail nie zostały włączone: w HA nie ma obecnie skonfigurowanej
  integracji/encji SMTP ani wskazanego odbiorcy. Wynik pozostaje w panelu agenta.
- Weryfikacja przed odbiorem: składnia skryptu JavaScript UI sprawdzona,
  odczyt zwrotny zmian na gałęzi potwierdzony. Nie wykonano pełnego zestawu
  testów repozytorium ani odbioru wizualnego w HA; wersje wymagają instalacji
  ręcznej i odbioru operatora.

## 0.39.20 — zakres, uwagi operatora i rozdzielenie trybów HP

- Interaktywne analizy używają domyślnie ostatnich 7 dni; jawnie wskazany
  okres w pytaniu jest respektowany w zakresie 1–28 dni.
- Agent odpowiada na konkretne pytanie bez stałej checklisty. Uwagi z
  odrzuconych TODO są informacją zwrotną; odrzucony wniosek wraca tylko przy
  nowym, wskazanym dowodzie.
- Kontekst i audyt Observera rozdzielają tryby HP_HEAT_DHW oraz HP_DHW:
  planowane okno grzania, prognozę CWU, rzeczywiste ogrzewanie i rzeczywiste
  CWU. Zużycie CWU samo w sobie nie jest dowodem pracy trybu grzania.
- Zmiany dotyczą analizy i prezentacji dowodów; planer i PPD pozostają bez zmian.

## 0.39.19 — cel końca doby i niezależna rezerwa przed PV/BUY

- Historyczny cel SOC z 7/14/28 dni obowiązuje tylko na końcu doby i ma
  tolerancję 5 punktów procentowych. Usunięto jego ponowne wymuszanie przed
  porannym SELL.
- Dodano niezależny bilans bezpieczeństwa: minimum 15% plus konfigurowalny
  bufor (domyślnie 2 p.p.), zużycie domu/HP, sprawności i niepewność prognoz.
  Potrzeby nocne mogą podnieść cel końca doby ponad średnią minus 5 p.p.
- Najbliższe BUY uwzględnia limit mocy/czasu; PV tylko nadwyżkę po odbiorach
  ograniczoną mocą ładowania. Końcowy kontrakt odejmuje rzeczywisty przydział
  zakupu, a nie całe potencjalne okno.
- Rezerwa bezpieczeństwa pozostaje wymagana we wszystkich przebiegach.
  Fallback usuwa sprzedaż przed obniżeniem niewykonalnego celu historycznego;
  nie może znieść rezerwy. Niewykonalna rezerwa daje jawny błąd zamiast
  publikacji niebezpiecznego wariantu.
- Testy obejmują przejście północy, poranny SELL, krótkie i pominięte BUY,
  opóźnione PV, moc ładowania, HP, niewykonalność i stabilność przeliczeń.
- Weryfikacja: 320 testów OK, `compileall` i `git diff --check` OK.
  Dodatkowo wykonano izolowane porównanie bilansu SOC na migawce 130
  aktualnych slotów HA; nie jest to pełny odbiór planera ani wykonania.
- Zmiana wymaga instalacji dodatku; odbiór na produkcji nie został wykonany.

## AI Observer Worker 0.1.4 — naprawa niepoprawnego JSON

- Gdy model zwróci niepoprawny JSON albo odpowiedź niezgodną z kontraktem,
  worker wykonuje jedną próbę naprawy formatu i ponownie waliduje wynik.
- Niepoprawna odpowiedź nie jest wykonywana ani zapisywana do Observera.
  Po nieudanej naprawie worker zapisuje w logu przyczynę walidacji; kolejny
  cykl może ponowić analizę, ponieważ watermark nie jest przesuwany.
- Dodano testy udanej i nieudanej naprawy odpowiedzi.
- Wersja Core pozostaje 0.39.18; wersja workera: 0.1.4.

## 0.39.18 — siedmiodniowa analiza Observera

- Observer Core pobiera maksymalnie 7 dni historii. Przeglądy zdarzeń obejmują
  tylko sloty zapisane po watermarku poprzedniego przebiegu, aby nie ponawiać
  tych samych historycznych błędów; trendy nadal liczone są z całego okna.
- Worker prosi o kontekst 7-dniowy podczas przeglądu cyklicznego i zapamiętuje
  watermark slotów. Odpowiedzi na pytania operatora zachowują kontekst do 28 dni.
- Obie ścieżki pozostają tylko do odczytu. Wersje: Core 0.39.18, Worker 0.1.3.

## 0.39.17 — TODO dostępne w kontekście projektu

- Konektor Core udostępnia filtrowanie listy TODO po statusie, w tym
  `ACCEPTED`, aby czaty projektu mogły pobrać sugestie skierowane do analizy.
- Read-only kontekst agenta zawiera do 25 TODO z opisem, dowodami, statusem
  oraz notatką operatora. Szczegóły są ograniczone, aby utrzymać odpowiedź
  w rozsądnym rozmiarze. Worker przekazuje je dalej do analizy.
- `ems_gpt_core_todo` pozostaje jedynym źródłem prawdy; nie powstaje druga lista.
  Akceptacja kieruje sugestię do analizy i ewentualnej propozycji zmiany
  projektu. Nie uruchamia zmian Core, planera ani urządzeń.
- Dokumentacja wskazuje ścieżkę konektora
  `api/todo?status=ACCEPTED&limit=10` dla kolejnych czatów EMS-GPT.
- Wersja Core: 0.39.17; Worker: 0.1.2. Dodano test filtra API oraz testy
  obecności TODO w kontekście Core i workera.

## 0.39.16 — ciągłość prognozy PV przy zmianie miesiąca i awarii źródła

- Prognoza slotowa używa kompletnego profilu bieżącego miesiąca lub,
  przy jego braku, najbliższego kompletnego miesiąca z historii. Wybór
  uwzględnia cykliczny kalendarz grudzień–styczeń i jest zapisywany w audycie.
- Zachowano osobne prognozy i korekty PV1/PV2 oraz ich sumy energii.
- Przy chwilowym braku źródła PV lub profilu zachowywana jest kompletna
  prognoza zapisana w SQL. Brakujące sloty mogą zostać odtworzone z ostatniej
  poprawnej migawki zaakceptowanego, opublikowanego planu dla tych samych
  znaczników czasu. Dane wykonania i kompletne prognozy nie są nadpisywane.
- Planer odrzuca brakujące, ujemne, nienumeryczne lub niespójne prognozy PV
  przed obliczeniami i publikacją. Rzeczywiste zero źródła jest dozwolone;
  brak danych nie jest zamieniany na zero.
- Testy obejmują wszystkie zmiany miesiąca, sumy PV1/PV2, zachowanie SQL,
  odzyskanie właściwej migawki oraz walidację danych wejściowych.
- Nie zmieniono algorytmu SOC, ekonomiki ani zasad BUY/SELL; zachowano
  poprawkę odczytu minionych okien HP z 0.39.15.

## 0.39.15 — odblokowanie planera przy brakach historii HP

- Usunięto krytyczną blokadę `INVALID_BOOLEAN:heat_pump_window:None`
  podczas odczytu minionych slotów ogrzewania.
- Do wykonanego ogrzewania zaliczane są wyłącznie znane okna opublikowanego
  planu. Slot nieopublikowany lub z brakującą flagą HP nie daje zaliczenia
  ogrzewania i nie przerywa kolejnego planowania.
- Braki są jawnie rejestrowane w `hp_elapsed_plan_history_missing`; odczyt
  nie zmienia historii i nie osłabia walidacji flag bieżącego planu.
- Dodano test regresyjny odczytu SQL i dalszej optymalizacji ogrzewania,
  granic dnia/slotu, nieopublikowanych okien i odrzucania błędnych flag.
- Zachowano ekonomię sprzedaży baterii i limit importu według wymaganego SOC.

## 0.39.14 — limit importu według wymaganego SOC

- Egzekutor podczas aktywnego ładowania sprawdza `soc_charge_target_pct`
  (z aliasem zgodnościowym `soc_target_pct`) i zatrzymuje import natychmiast
  po osiągnięciu wymaganego celu, niezależnie od wyższego `soc_end_plan_pct`.
- Brak targetu w opublikowanym planie bezpiecznie zatrzymuje import. Kontrola
  eksportu nadal używa osobnego końcowego SOC slotu.
- Zachowano kontrolę ekonomii `SELL_BAT` z 0.39.13. Testy używają
  przykładowych danych bez stawek przedstawianych jako reguły produkcyjne.
- Dodano regresję dla rozbieżności między targetem ładowania a SOC końcowym.

## 0.39.12 — rozszerzony audyt AI Observera

- Observer uruchamiany po analizie okresowej przegląda do 28 dni zamkniętych
  slotów oraz do 96 przyszłych slotów planu.
- Dodano audyt eksportu przy cenie <= 0, importu poza BUY, powtarzalnego
  niedoszacowania PV1/PV2/PV łącznie i zużycia, okien HP oraz SOC na zamknięciu
  doby i zgodności planu z wymaganym SOC.
- TODO zawiera zakres sprawdzonych danych, wykryty błąd, próg, sloty/pomiary,
  wniosek i rekomendację; alarm krytyczny trafia od razu do statusu OPEN.
- Dodano osobny worker LLM uruchamiany poza Core. Po nowym przebiegu analityki
  zapisuje analizę do AI Observer/TODO; obsługa skrzynki pytań pozostaje
  odrębną funkcją.
- Worker i Core Observer pozostają `SHADOW_READ_ONLY`: nie zapisują planu, PPD,
  ustawień ani poleceń. Wymagają osobnej konfiguracji i nie uruchamiają się
  automatycznie po ustawieniu tokenu API.
- Testy obejmują wykrywanie naruszeń, próg szumu technicznego i wymóg
  powtarzalności dla niedoszacowania prognozy.

## 0.39.11 — kanał komunikacji z agentem AI

- Dodano trwałą skrzynkę pytań i odpowiedzi operatora z osobnym uwierzytelnieniem
  tokenem agenta. Agent może pobierać wiadomości, odczytywać ograniczony
  kontekst przyszłych slotów, wykonania, analityki i Observera oraz zapisywać
  wyłącznie odpowiedź na przejęte pytanie.
- Agent nie otrzymuje adapterów planera, PPD, wykonawcy, usług Home Assistant,
  poleceń ani konfiguracji sterowania. Kontekst ma jawny tryb tylko do odczytu.
- Dodano zakładkę `Agent AI` do panelu operatora oraz testy cyklu wiadomości
  i izolacji uprawnień.
- Sam token włącza API agenta, ale nie uruchamia modelu ani procesu worker.
  Kontrakt integracji opisano w `docs/AI_AGENT.md`.

## 0.39.10 — rzeczywista nadwyżka mimo rozbieżności planu SOC

- PPD uwzględnia świeży pomiar PV, zużycia i SOC przy otwieraniu najbliższego
  okna CWU/EV, gdy planowany SOC lub przydział nadwyżki odbiega od pomiaru.
  Prognoza nadal wyznacza dalszy plan.
- Executor może uruchomić CWU/EV w bieżącym slocie przy rzeczywistej nadwyżce
  i osiągniętym target SOC, nawet gdy wcześniejsza prognoza oznaczyła slot
  jako `BLOCK`. Zachowuje progi mocy, histerezę, ręczne `OFF` i wymóg świeżych
  danych.
- Skrypt Home Assistant kończący import nie zapisuje już sztywnych wartości
  SOC 20/20/40/40/40/30. Przywracanie korzysta z konfiguracji executora.

## 0.39.9 — prognoza nie blokuje odbioru rzeczywistej nadwyżki

- Wspólne okno `PV_CWU` i `PV_EV` otwiera się po osiągnięciu targetu,
  gdy plan wykazuje istotną dodatnią nadwyżkę PV. Nie wymaga już, aby
  prognoza slotu przekraczała próg startowy CWU lub EV.
- Progi 2,0/1,5 kW, pierwszeństwo CWU, histereza, świeża telemetria
  i kontrola bieżącego SOC pozostają w wykonawcy. Słaba prognoza nie
  zablokuje odbiorników przy rzeczywistej nadwyżce i niedodatniej cenie
  sprzedaży.

## 0.39.8 — wspólne okno PV_CWU/PV_EV i priorytet runtime

- PPD publikuje wspólne okno dopuszczenia odbiorów elastycznych po osiągnięciu
  targetu. Osobne progi prognozy nie mogą już dać EV=ALLOWED przy CWU=BLOCKED.
- Wewnątrz wspólnego okna executor stosuje rzeczywistą telemetrię i osobne
  progi ON/OFF: najpierw CWU 2,0 kW, następnie EV 1,5 kW z pozostałej mocy.
- Zachowano histerezę, tryby ręczne oraz brak wpływu CWU/EV na target i load.

## 0.39.7 — odbiory PV po osiągnięciu targetu

- Po osiągnięciu opublikowanego targetu runtime guard liczy energię dostępną
  dla `PV_CWU/PV_EV` jako `PV - load`, bez odejmowania dalszego ładowania
  baterii ponad target.
- Włączenie CWU/EV naturalnie zmniejsza moc ładowania baterii; target pozostaje
  bramą odczytywaną przez executor i nie jest modyfikowany.
- Zachowano kolejność CWU przed EV, progi 2,0/1,5 kW oraz histerezę.
- Nie zmieniono obliczeń SOC target, SOC required, floor ani trajektorii SOC.

## 0.39.6 — niezależne procesy sprzedaży baterii i PV

- Zastąpiono niejednoznaczną nazwę procesu PPD `BATTERY_EXPORT` nazwą
  `SELL_BAT`; istniejące rekordy procesów są migrowane przy starcie.
- Dodano niezależny proces informacyjny `SELL_PV`. Każdy z procesów sprzedaży
  ma dwie osobne osie: politykę `ALLOWED/BLOCKED` oraz wykonanie `ON/OFF`.
  Dodatnia cena pozwala na `SELL_PV=ALLOWED`, a cena mniejsza lub równa zero
  daje `SELL_PV=BLOCKED`; obecność nadwyżki niezależnie ustala ON/OFF.
- `SELL_PV` nie uruchamia skryptów baterii. Dotychczasowe skrypty eksportu
  baterii są przypisane wyłącznie do `SELL_BAT`.
- Sprzedaż baterii nie przecina już okna `PV_CWU/PV_EV`; odbiory elastyczne
  korzystają z niezależnej nadwyżki PV i zachowują priorytet przed sprzedażą PV.
- Nie zmieniono targetu, required, floor ani trajektorii SOC.

## 0.39.5 — przywrócenie priorytetu nadwyżki PV

- Przywrócono stałą kolejność wykorzystania PV: odbiory domu wraz z HP,
  ładowanie baterii, PV_CWU, PV_EV, a dopiero potem eksport pozostałego PV.
  Cena możliwego eksportu PV nie może już blokować PV_CWU ani PV_EV.
- Sprzedaż baterii jest bezwzględnie wykluczona przy cenie sprzedaży mniejszej
  lub równej zero: podczas wyznaczania okien, w optymalizatorze oraz w końcowej
  walidacji opublikowanego przepływu.
- Nie zmieniono obliczeń SOC target, SOC required, podłóg ani trajektorii SOC.
- W trybie AUTO wykonawca przepuszcza `PV_CWU` i `PV_EV` dopiero po kontroli
  świeżej telemetrii: rzeczywistego PV, load, ładowania baterii i osiągnięcia
  opublikowanego targetu. CWU ma pierwszeństwo, EV otrzymuje wyłącznie
  pozostałość, a brak danych lub zanik nadwyżki wymusza stan OFF.
- Dodano histerezę wykonawczą oraz osobne wersjonowanie przejść ON/OFF w slocie.
  Odbiory elastyczne pozostają poza load i nie zmieniają żadnej wartości SOC.

## 0.39.4 — ekonomika sprzedaży, przywracanie SOC i historia targetu

- Plan z `SELL_BAT` przechodzi dodatkową symulację bez sprzedaży przy tym samym
  horyzoncie, prognozie i wymaganym SOC. Weryfikacja uwzględnia wybrane sloty
  ładowania i ich ceny, wartość energii pozostającej w baterii oraz dodatkowy
  import dla domu. Niekorzystny wariant jest ponownie planowany bez sprzedaży.
- Wynik i przyczyna odrzucenia są zapisywane w zdarzeniu
  `battery_sale_counterfactual`.
- Po restarcie znacznik przywracania programu falownika nie zawiera już
  dawnego SOC. Executor odczytuje obowiązującą wartość z bieżących opcji
  dodatku; stary plik z zapisanym `20%` jest traktowany jako sam znacznik.
- Nieukończony, ciągły horyzont historii targetu otrzymuje stan `OCZEKUJE`,
  pozostaje w zakładce historii i nie zwiększa licznika próbek nieważnych.
  Przerwany horyzont nadal jest oznaczany jako nieważny.

## 0.39.3 — bazowe zużycie bez HP i historyczna moc ogrzewania

- Awaryjna prognoza zużycia bazowego odejmuje rzeczywistą energię pompy ciepła
  i EV, więc odbiory sterowane nie są drugi raz dodawane do bilansu planera.
- Planowana moc ogrzewania korzysta z ważonej średniej rzeczywistych aktywnych
  slotów HP z okien 7/14/28 dni, z tymi samymi wagami co target SOC.
- Przy braku historii startowy fallback HP wynosi 1,5 kW. Planer zapisuje źródło,
  liczbę próbek i wynikową moc w zdarzeniu audytowym.

## 0.39.2 — terminalny fallback we wszystkich przebiegach planera

- Wspólna obsługa niewykonalnego terminalnego SOC obejmuje teraz także iteracje
  zabezpieczeń TOU, ich końcową walidację oraz przebieg `TARGET_COMMITMENT`.
- Każdy z tych etapów najpierw ponawia obliczenia bez `SELL_BAT`, a dla bieżącej
  doby publikuje najwyższy fizycznie osiągalny SOC zamiast wycofywać cały plan.
- Dodano nazwę etapu do zdarzeń audytowych, aby kolejna niewykonalność wskazywała
  dokładne miejsce bez analizy stosu wyjątków.

## 0.39.1 — przywrócenie fallbacku terminalnego SOC

- Błąd `No feasible terminal SOC state for complete horizon` ponawia plan bez
  `SELL_BAT`, przywracając zasadę bezpieczeństwa wprowadzoną w 0.38.3.
- Jeżeli po wyłączeniu sprzedaży cel końcowy bieżącej doby nadal jest fizycznie
  nieosiągalny, planer publikuje najwyższy osiągalny SOC i jawny niedobór zamiast
  wycofywać cały plan. Dzięki temu może opublikować także `HP_HEAT_DHW`.
- Cele końcowe przyszłych dób pozostają twarde i nie korzystają z fallbacku.

## 0.39.0 — jedno źródło konfiguracji i ochrona porannego SOC

- Zastąpiono `deye_program_soc_baseline_json` sześcioma osobnymi opcjami
  dodatku. Programy 1–5 mają wartość 10%, program 6 — 30%. Panel aplikacji nie
  przechowuje drugiej kopii tych parametrów, a dawna wartość runtime jest
  jawnie ignorowana.
- Encja rzeczywistej temperatury ogrodu jest opcją dodatku
  `garden_temperature_entity`; nie jest już wpisana na sztywno w `app.py`.
- Widok Daily nie wykonuje globalnego zliczania wszystkich dób i nie pokazuje
  pól `Doby razem`, `Doby do nauki` ani `Doby poza nauką`.
- Przed rozpoczęciem porannego okna SELL planer wymaga historycznego targetu
  końcowego SOC danej doby. Bilans wsteczny zachowuje dodatkowo energię na
  nocne odbiory, dlatego target północy nie może zostać zużyty przed porannym
  szczytem RCE.

## 0.38.9 — ochrona SOC po sprzedaży i kontrolowany most do BUY

- Sprzedaż z baterii zachowuje bazowy SOC aktywnego programu TOU oraz energię
  potrzebną odbiorom do chwili wejścia kolejnego, niższego programu. Planner
  nie może już sprzedać dokładnie do 40% i wymusić później importu dla domu.
- Programy 5 i 6 mogą zostać tymczasowo obniżone wyłącznie wtedy, gdy ten sam
  opublikowany plan zawiera rzeczywisty zakup do baterii w ciągu maksymalnie
  180 minut oraz bezpieczne zamknięcie doby co najmniej na `soc_required`.
- Executor prowadzi próg programu 5/6 za ilościową trajektorią SOC, wyłącza
  ładowanie programu podczas mostu i przywraca bazowe wartości po utracie
  warunków, zmianie planu lub zakończeniu zakupu.

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
  ładowaniem baterii.

## 0.38.4

- Konfiguracja aplikacji zawiera teraz dziesięć jawnych odniesień do skryptów
  `ON/OFF` executora dla `BATTERY_IMPORT`, `BATTERY_EXPORT`, `PV_CWU`, `PV_EV`
  oraz `HP_HEAT_DHW`. Pola mają domyślne wartości odpowiadające istniejącym
  encjom `script.ems_gpt_core_*` i można je edytować w panelu konfiguracji.
- Executor korzysta z jawnych pól jako źródła prawdy. Dotychczasowy
  `connector_service_map_json` pozostaje wyłącznie fallbackiem migracyjnym.

## 0.38.3

- Jeżeli sprzedaż z baterii koliduje z historycznym celem SOC końca doby,
  planer najpierw przelicza cały horyzont bez `SELL_BAT`. Bateria nadal może
  zasilać odbiory domu; blokowany jest wyłącznie celowy eksport jej energii.
- Replan po ostatnim wykonalnym oknie uzupełnienia nie odrzuca już całego
  planu, gdy historyczny cel końca bieżącej doby stał się fizycznie
  nieosiągalny. Cel pozostaje publikowany, a planer zapisuje jawny shortfall i
  wybiera najlepszą wykonalną trajektorię.
- Zwolnienie niewykonalnej granicy dotyczy wyłącznie konkretnej doby; cele
  kolejnych dób pozostają twardymi ograniczeniami.

## 0.38.2

- Historyczna prognoza końcowego SOC 7/14/28 jest twardą minimalną granicą
  zamknięcia każdej doby w horyzoncie, a nie wyłącznie ostatniej doby planu.
  Przy obecnej średniej około 40% planer nie może publikować końca dnia około 20%.
- BUY pozostaje zakupem energii do baterii: nie może powstać wyłącznie dla
  zużycia domu, a energia ładowania musi być co najmniej rów…9058 tokens truncated…tyki: mianownik obejmuje teraz sloty z planem opublikowanym przez EMS-GPT Core.
- Historyczne sloty importowane bez planu Core nadal uczestniczą w dostępnych metrykach, ale nie są błędnie traktowane jako braki Core.
- Brak danych w opublikowanym slocie nadal obniża wynik; liczebność okna jakości jest zapisywana w szczegółach przebiegu.

## 0.26.16
- Wydzielono wartości domyślne i ładowanie konfiguracji do modułu `config_service.py`.
- Zachowano kolejność nadpisywania: wartości domyślne → opcje dodatku → ustawienia runtime.
- Dodano testy wartości domyślnych, priorytetu ustawień runtime i izolacji konfiguracji.

## 0.26.15
- Wydzielono obliczanie czasu lokalnego i początku slotu z `app.py` do modułu `time_service.py`.
- Zachowano dotychczasową konwersję strefy czasowej i zaokrąglanie do konfigurowalnej długości slotu.
- Dodano testy granic slotów, konwersji UTC do Europe/Warsaw i walidacji długości slotu.

## 0.26.14

- Spolszczono techniczne statusy w panelu: `CONNECTED` jest prezentowane jako `POŁĄCZONY`, a `LIVE` jako `PRODUKCJA`.
- Zmieniono etykietę i potwierdzenie przycisku wykonawcy na tryb `PRODUKCJA`.
- Wartości kontraktu API pozostają bez zmian dla zgodności wykonawcy i diagnostyki.

## 0.26.13

- Wydzielono bootstrap starszych tabel i odtwarzanie przerwanych przebiegów do `recovery_service.py`.
- Zachowano idempotentne oznaczanie starych przebiegów jako `ABORTED_RECOVERED` oraz audyt diagnostyczny.
- Migracja ze źródłowej bazy pozostaje domyślnie wyłączona i korzysta z walidowanych nazw SQL.
- Bez zmian w planerze, PPD, wykonawcy, obserwatorze i programach SOC 1–6.

## 0.26.12

- Wydzielono połączenie z MariaDB i walidację identyfikatorów SQL do `database_service.py`.
- Zachowano transakcje commit/rollback, timeouty oraz ustawianie strefy czasowej sesji bazy.
- `app.py` korzysta z jednego współdzielonego adaptera bazy dla wszystkich modułów.
- Bez zmian w schemacie, planerze, PPD, wykonawcy, obserwatorze i programach SOC 1–6.

## 0.26.11

- Wydzielono współdzielony stan procesu i blokadę ciężkich zadań do `runtime_service.py`.
- Harmonogram, API i inicjalizacja nadal korzystają z jednego obiektu stanu i jednej blokady wykonania.
- Zachowano kontrakt odtwarzania, statusy modułów i ostrzeganie o oczekiwaniu na blokadę.
- Bez zmian w planerze, PPD, wykonawcy, analityce, obserwatorze i programach SOC 1–6.

## 0.26.10

- Wydzielono komunikację z Home Assistantem do `ha_gateway_service.py`.
- Odczyt stanów, wywołania usług i konwersja wartości zachowują dotychczasowe timeouty oraz obsługę błędów.
- Odczyt sześciu programów Deye TOU pozostaje wyłącznie do odczytu; programy SOC 1–6 nie są modyfikowane.
- Bez zmian w planerze, PPD, wykonawcy, analityce i obserwatorze.

## 0.26.9

- Wydzielono kanoniczny kalendarz slotów i backfill relacji do `slot_calendar_service.py`.
- Zachowano obsługę dni DST z 92, 96 albo 100 rzeczywistymi slotami oraz jednoznaczne `slot_id` w UTC.
- Dodano testy długości doby przy zmianie czasu w Europie/Warszawie.
- Bez zmian w telemetrii, planerze, PPD, wykonawcy i programach SOC 1–6.

## 0.26.8

- Wydzielono zamykanie slotów, szczegóły wykonania, agregaty i odbudowę po restarcie do `materialization_service.py`.
- Zachowano kolejność: telemetria → zamknięcie slotu → szczegóły wykonania → agregaty godzinowe/dobowe.
- Usługa otrzymuje jawne adaptery bazy, zegara, konfiguracji i audytu.
- Bez zmian w planerze, PPD, wykonawcy i programach SOC 1–6.

## 0.26.7

- Wydzielono równoległy odczyt encji Home Assistant i zapis próbek do `telemetry_service.py`.
- Zachowano mapowanie encji, normalizację W/kW/MW, znak mocy baterii i relację do kanonicznego slotu DST.
- `app.py` przekazuje telemetrii jawne adaptery źródeł, zegara i bazy.
- Bez zmian w zamykaniu slotów, planerze, PPD, wykonawcy i programach SOC 1–6.

## 0.26.6

- Dodano kafelek RCE w panelu ze statusem, dniem docelowym, liczbą slotów i wynikiem planera.
- Odtworzenie dziennego wyniku RCE po restarcie jest teraz widoczne także w logu dodatku.
- Bez zmian w przebiegu RCE, cenach, planerze, PPD, wykonawcy i programach SOC 1–6.

## 0.26.5

- Dodano jawny stan ostatniego przebiegu RCE do `/api/status`: wynik, dzień docelowy, liczba slotów i status planera.
- Po restarcie stan RCE jest odtwarzany z dziennego zdarzenia sukcesu, bez ponawiania już zakończonego importu.
- Log dodatku zapisuje rozpoczęcie, zakończenie albo błąd automatycznego importu RCE.
- Niepełna doba pozostaje `PARTIAL` i nie uruchamia publikacji planu; wyjątek ustawia stan RCE na `ERROR`.
- Bez zmian w cenach, oknach, planerze, PPD, wykonawcy i programach SOC 1–6.

## 0.26.4

- Wydzielono pobieranie prognoz PV, prognozy pogody i cen RCE do `ingestion_service.py`.
- Usługa danych otrzymuje jawne adaptery bazy, zegara, kalendarza DST, Home Assistant i audytu.
- Zachowano dotychczasowe źródła Open-Meteo i PSE oraz identyczne reguły wyznaczania okien zakupu i sprzedaży.
- Bez zmian w planerze, PPD, wykonawcy i programach SOC 1–6. Observer pozostaje `SHADOW_READ_ONLY`.

## 0.26.3

- Wydzielono ustawienia operatora, override'y procesów i cykl życia komend do `executor_service.py`.
- `app.py` przekazuje wykonawcy jawne adaptery stanu, bazy, zegara, Home Assistant i audytu.
- Zachowano dotychczasowy allowlist skryptów, potwierdzenie aktywacji, TTL komend i blokadę eksportu poniżej aktywnego floor TOU.
- Bez zmian w harmonogramie, planerze, PPD i programach SOC 1–6. Observer pozostaje `SHADOW_READ_ONLY`.

## 0.26.2

- Wydzielono serwer HTTP i komplet endpointów Ingress do `api_service.py`.
- `app.py` buduje handler z jawnych adapterów, pozostając koordynatorem uruchomienia.
- Zachowano identyczne ścieżki GET/POST, limity odpowiedzi, kontrolę zdrowia i nagłówek użytkownika Ingress.
- Panel nadal jest dostarczany z osobnego `webui.html`.
- Bez zmian w harmonogramie, planerze, PPD, wykonawcy i programach SOC 1–6.

## 0.26.1

- Wydzielono obliczenia analityczne do `analytics_service.py`; kontrakt metryk i zapisy SQL pozostały bez zmian.
- Wydzielono minutowy harmonogram do `scheduler_service.py` z jawną listą adapterów operacji.
- `app.py` koordynuje uruchomienie usług i zachowuje dotychczasowe nazwy funkcji używane przez API.
- Zachowano częstotliwości: telemetria co minutę, replan w minutach 07/22/37/52, analityka raz na godzinę oraz diagnostyka cztery razy na dobę.
- Bez zmian w PPD, programach SOC 1–6, oknach procesów i trybie wykonawcy.

## 0.26.0

- Rozpoczęto modularizację rdzenia: panel WWW przeniesiono do osobnego zasobu, a Observer, diagnostykę i cykl życia TODO do niezależnych usług.
- `app.py` pozostaje koordynatorem zgodności dla harmonogramu i API; publiczne endpointy oraz wywołania nie zmieniły nazw.
- Usługi otrzymują jawne adaptery bazy, zegara i audytu, co ogranicza sprzężenie z serwerem HTTP i ułatwia osobne testowanie.
- Obraz dodatku kopiuje wszystkie moduły Pythona i plik panelu.
- Bez zmian w PPD, planerze, programach SOC 1–6 i wykonawcy. Observer nadal działa wyłącznie jako `SHADOW_READ_ONLY`.

## 0.25.20

- Wszystkie komendy `READY_FOR_CONNECTOR`, `DISPATCHED` i `ACCEPTED` po przekroczeniu TTL są atomowo zamykane jako `EXPIRED`; diagnostyka nie raportuje już historycznych komend jako aktywnych.
- Sugestie Observera mają trwały cykl życia. Pierwsze dwa kolejne dni mają status `WATCHING`, a dopiero trzeci kolejny dzień tego samego problemu podnosi wpis do `SUGGESTED`.
- Znikające obserwacje są archiwizowane, a po północy archiwizowane są również rozpatrzone i nieaktualne wpisy.
- Nieaktualne alarmy diagnostyczne są automatycznie oznaczane jako `RESOLVED` po pierwszym raporcie, w którym problem już nie występuje.
- Dodano audytowalne decyzje operatora `ACCEPTED`, `REJECTED` i `RESOLVED` przez endpoint `/api/todo/review`.
- Observer pozostaje `SHADOW_READ_ONLY`; nie zapisuje planu, PPD, komend ani usług Home Assistant. Programy SOC 1–6 pozostają chronione.

## 0.25.19

- Ograniczono pełną odbudowę 7 dni z wykonywania co minutę do startu aplikacji i jednego przebiegu dziennie około 01:00.
- Zserializowano ciężkie zadania bazy danych uruchamiane przez silnik i ręczne endpointy.
- Naprawiono harmonogram analityki, który wcześniej sprawdzał minutę początku slotu i nie mógł trafić w minutę 8.
- Watchdog HTTP uwzględnia teraz wiek heartbeat i uznaje silnik za niesprawny po 180 sekundach bez aktualizacji.
- Bez zmian w PPD, wykonawcy i programach SOC 1–6.

## 0.25.17

- Połączono plan okna HP z bilansem energii i ścieżką SOC baterii.
- Energia HP wpływa na floor, target, rozładowanie oraz plan taniego doładowania.
- Dodano test regresyjny zapobiegający ponownemu rozdzieleniu planu HP od SOC.

## 0.25.16

- Naprawiono pobieranie godzinowej prognozy z Home Assistant: `weather.get_forecasts` jest wywoływane z wymaganym `return_response`.
- Parametr odpowiedzi jest używany wyłącznie dla usług, które zwracają dane; wywołania skryptów wykonawcy pozostają bez zmian.
- Wykonawca pozostaje domyślnie wyłączony.

# EMS-GPT Core — changelog

## 0.25.8 — poprawny kafelek stanu aplikacji

- usunięto informację o pompie cyrkulacyjnej z górnego kafelka `Stan`;
- kafelek `Stan aplikacji` pokazuje status EMS-GPT Core, wersję i czas ostatniego heartbeat;
- odczyt stanu pompy pozostaje dostępny przez API procesu, bez eksponowania go w kafelku aplikacji.
- przy każdym uruchomieniu aplikacja przechodzi w tryb produkcyjny `LIVE`, jeżeli kompletny bezpieczny zestaw skryptów wykonawczych jest dostępny;
- operator nadal może wyłączyć wykonawcę (`OFF`) i ponownie włączyć go (`LIVE`) z kafelka;
- brak pełnego mapowania skryptów powoduje bezpieczny start `OFF` zamiast częściowego sterowania.

## 0.25.7

- Planer i wykonawca respektują sprzętowy próg SOC aktywnego programu TOU Deye.
- Niewykonalna sprzedaż baterii jest blokowana z jawną diagnostyką bez zapisu programów SOC 1–6.

## 0.25.6 — uproszczenie panelu cyrkulacji

- usunięto z karty `MANUAL_CIRCULATION` dodatkową kontrolkę stanu pompy;
- rzeczywisty stan pompy przeniesiono do górnego kafelka `Stan`;
- kafelek pokazuje `DZIAŁA`, `WYŁĄCZONA` albo `BRAK DANYCH` i odświeża się co 5 sekund;
- pozostawiono przyciski procesu `Włącz / Blokuj / Auto`;
- encja `switch.sm_lite_1616r_2_pompa_cyrkulacyjna` oraz odczyt API pozostają dostępne poza kartą.

## 0.25.5 — stan cyrkulacji i poprawny status wykonawcy

- karta `MANUAL_CIRCULATION` pokazuje rzeczywisty stan przekaźnika pompy;
- wskaźnik rozróżnia `DZIAŁA`, `WYŁĄCZONA` oraz `BRAK DANYCH`;
- stan encji `switch.sm_lite_1616r_2_pompa_cyrkulacyjna` jest odświeżany co 5 sekund;
- aktywny i potwierdzony wykonawca pokazuje teraz `LIVE`, zamiast mylącego `CONNECTOR_REQUIRED`.
- naprawiono adapter usług HA: `script.turn_on` nie żąda już nieobsługiwanych danych zwrotnych, które powodowały `HTTP 400`;
- wyłączony wykonawca pokazuje teraz jednoznacznie `OFF`.

## 0.25.4 — porządek kart operatora

- usunięto kartę `HP_DHW` z panelu Procesy;
- proces `HP_DHW`, jego API, historia i skrypty „Wymuś ciepłą wodę” pozostają dostępne w backendzie;
- karta `HP_HEAT_DHW` nadal obsługuje automatyczne przełączenie `Heat+DHW` i powrót do `DHW only`.
- karta „Wykonawca” po kliknięciu udostępnia szybkie `Włącz LIVE` i `Wyłącz`;
- aktywacja LIVE wymaga potwierdzenia, kompletnego mapowania skryptów i jest zapisywana trwale bez restartu.

## 0.25.3 — właściwa polityka pompy ciepła

- Stan bazowy pompy pozostaje `DHW only`.
- `HP_HEAT_DHW` przełącza na `Heat+DHW` tylko w skonfigurowanym oknie HP, gdy minimalna prognozowana temperatura nocna 00:00–06:00 jest niższa od parametru `Nocny próg ogrzewania [°C]`.
- Brak prognozy temperatury działa fail-closed i blokuje automatyczne `Heat+DHW`.
- Po zakończeniu okna decyzja `OFF` przywraca `DHW only`.
- `HP_DHW ON/OFF` zachowuje osobną obsługę `Wymuś ciepłą wodę` i jest procesem wyłącznie na żądanie operatora.
- Executor pozostaje domyślnie w `DRY_RUN`; programy SOC 1–6 nie są modyfikowane.

## 0.25.2 — siódmy proces i komplet wykonawczy

- Dodano proces `HP_HEAT_DHW` jako niezależny od `HP_DHW`, z trwałymi decyzjami, override `AUTO/FORCE_ON/FORCE_OFF`, przebiegiem i kartą w panelu.
- `HP_HEAT_DHW` pozostaje domyślnie `ON_DEMAND`; automatyczna polityka godzinowa wymaga osobnego odbioru i nie jest aktywowana w tej wersji.
- Przygotowano mapowanie do osobnych, idempotentnych skryptów CORE `ON` i `OFF`; executor po aktualizacji nadal pozostaje w `DRY_RUN`.
- Naprawiono serializację dat w zdarzeniach override (`datetime is not JSON serializable`).
- Zachowano dynamiczne pobieranie marży zakupu z konfiguracji; `0,59 PLN/kWh` jest wyłącznie wartością domyślną.

## 0.25.1 — konfiguracja stałych operacyjnych

- Usunięto ostatnie wykonawcze zależności telemetrii od V1/V2/V3: RCE jest odczytywane z kanonicznego rekordu `ems_gpt_slots`, a moc baterii bezpośrednio z falownika Deye.
- Zachowano migrację starej opcji `legacy_helpers`; jest interpretowana jako `discharge_positive` i nie powoduje odczytu usuniętych helperów.
- Recovery SLOT/HOUR/DAILY obejmuje domyślnie 7 dni (konfigurowalne w panelu w zakresie 1–31 dni), a kolejka zamykania obsługuje do 2688 zaległych slotów.
- Brak bezpośredniego sensora baterii działa fail-closed: nie są tworzone zastępcze wartości ładowania ani rozładowania.

- Potwierdzono, że marża zakupu jest pobierana dynamicznie z ustawienia panelu; `0,59 PLN/kWh` pozostaje wyłącznie bezpieczną wartością domyślną dla nowej instalacji.
- Do panelu przeniesiono tolerancję okna zakupu, progi przepływów planowanych i technicznych oraz limity SOC floor/target.
- Konfigurowalne są okna sprzedaży oraz okna pracy HP dla dni roboczych i weekendów.
- Do panelu przeniesiono progi kompletności telemetrii, kwalifikacji do uczenia oraz progi ostrzeżeń AI Observera.
- Ujednolicono ocenę jakości SLOT, HOUR i backfillu tak, aby korzystała z jednego progu konfiguracyjnego.
- Pakiet przygotowany offline; bez publikacji, stagingu i restartu dodatku przed odbiorem RCE.

## 0.25.0 — migracja analiz, AI Observer i pełny horyzont RCE

- Rozszerzono analitykę V3 o bias PV/zużycia/importu/eksportu, błąd SOC oraz odchylenie wyniku PLN.
- Dodano AI Observer w trybie `SHADOW_READ_ONLY`: zapis wejścia, wyniku, autooceny i sugestii/TODO bez prawa zmiany planu, PPD lub urządzeń.
- Panel otrzymał osobny widok AI Observer oraz dodatkowe kolumny analityczne.
- Planer przetwarza wszystkie ciągłe przyszłe sloty z dostępnym RCE zamiast stałego limitu 96.
- Diagnostyka ocenia pełny dostępny horyzont RCE, z uwzględnieniem dób DST 92/96/100.
- Executor pozostaje wyłączony; brak zapisów do programów SOC 1–6.

## 0.24.3 — zgodność metadanych wersji

- Agregaty HOUR zapisują `source_version` wyliczany z bieżącej wersji aplikacji zamiast historycznej stałej `CORE_0_22_1`.
- Ujednolicono numer wersji runtime i manifestu lokalnego dodatku.
- Executor pozostaje wyłączony; brak zapisów do programów SOC 1–6.

## 0.24.2 — allocator PV w HOUR i DAILY

- Dodano osobne sumy PV→BAT, PV→CWU, PV→EV, PV→sieć i ograniczenia PV w HOUR.
- Dodano dobowe sumy PV→BAT, PV→CWU, PV→EV i ograniczenia PV w DAILY.
- Odbudowa po awarii wylicza pola ze SLOT bez imputacji wartości actual.
- Executor pozostaje wyłączony; brak zapisów do programów SOC 1–6.

## 0.24.1 — domknięcie audytu migracji

- Nowe rekordy wykonania procesów otrzymują kanoniczny `slot_id` już przy zapisie; nie wymagają restartowego backfillu.
- Przepływ baterii poniżej 0,050 kWh/slot jest traktowany jako techniczny i nie uruchamia obserwacji procesu importu/eksportu.
- Diagnostyka przed publikacją cen następnego dnia używa realnie dostępnego horyzontu do końca doby zamiast fałszywego wymagania 96 cen.
- Otwarte TODO diagnostyczne są deduplikowane per dzień, moduł i tytuł.
- Status AI Observer jest jawny: `DISABLED` albo `NOT_IMPLEMENTED`; sam kontrakt tabeli nie jest raportowany jako działający moduł.
- Executor pozostaje wyłączony; nie zmieniono programów SOC 1–6.

## 0.24.0

- Rozdzielono `SOC przed`, `SOC po`, `SOC floor` i `SOC target`; target nie jest już wymuszany do wartości floor.
- Końcowy przebieg SOC jest liczony sekwencyjnie, z ciągłością między sąsiednimi slotami.
- Dodano ilościowy allocator PV→BAT→CWU→EV→sieć/ograniczenie.
- Rozszerzono relacje tabel zależnych o kanoniczny `slot_id` i bezpieczny backfill danych historycznych.
- Naprawiono formatter panelu usuwający literę T z nazw BATTERY, NEUTRAL i podobnych wartości.

## 0.23.0

- Dodano kanoniczny kalendarz slotów oparty o UTC z `slot_id`, offsetem, foldem i lokalnym indeksem doby.
- Doby Europe/Warsaw mają automatycznie 92, 96 albo 100 slotów podczas zmian DST.
- RCE zachowuje ceny ujemne, oczekuje liczby slotów właściwej dla doby i jest ponawiane od 14:00 co 10 minut.
- Usunięto pełne uruchomienie planera o północy; nowy plan powstaje po kompletnym imporcie następnej doby RCE.
- Naprawiono kwalifikację procesu BATTERY_IMPORT dla polityki `BUY_ALLOWED`.
- Migracja jest addytywna: dotychczasowy `slot_start` pozostaje kluczem zgodności do osobnego odbioru przełączenia historycznych relacji SQL.

## 0.22.1 — recovery SLOT/HOUR/DAILY po awarii

- Zaległe sloty są przetwarzane chronologicznie w zakresie do 384 rekordów na cykl.
- Slot bez telemetrii po zakończeniu otrzymuje terminalny stan `MISSING_OUTAGE`; wartości actual pozostają `NULL`.
- Slot odtworzony z zachowanych próbek otrzymuje jawny stan `RECOVERED` i ocenę pokrycia.
- HOUR jest przebudowywany dla bieżącej i poprzedniej doby oraz zapisuje liczniki expected/terminal/recovered/missing, pokrycie, stan zamknięcia i bramę uczenia.
- DAILY jest przebudowywany dla obu dotkniętych dób, obsługuje 92/96/100 slotów DST, a `closed_at` jest ustawiane tylko po terminalnym zamknięciu zakończonej doby.
- Niepełne i brakujące sloty nie zasilają profili uczenia.
- Panel HOUR i DAILY pokazuje kompletność, jakość, odzyskanie, braki oraz kwalifikację do uczenia.
- Executor pozostaje wyłączony, a recovery nie steruje urządzeniami.

## 0.22.0 — kompletna, jawna macierz PPD

- Dodano trwałe, osobne flagi PPD dla sieci: `BUY_ALLOWED`, `NO_BUY` i `NEUTRAL`.
- Dodano rozdzielone decyzje sprzedaży baterii i PV: `SELL_BAT/NO_SELL_BAT` oraz `SELL_PV/NO_SELL_PV`.
- Dodano trzy wzajemnie wykluczające się stany pompy ciepła: `PREFERRED`, `NEUTRAL` i `AVOID`.
- Walidacja planu odrzuca publikację, jeśli którakolwiek grupa PPD nie jest kompletna lub jednoznaczna.
- Macierz PPD jest zapisywana zarówno w stagingu planera, jak i w opublikowanych slotach.
- Executor pozostaje wyłączony; migracja PPD nie uruchamia sterowania urządzeniami.

## 0.21.2

- Dodano tabelę „Przebiegi” z planem, stanem obserwowanym, energią i źródłem sterowania.
- Rozbieżność plan–obserwacja jest jawnie klasyfikowana; aktywność bez komendy CORE otrzymuje oznaczenie „ZEWNĘTRZNE / RĘCZNE”.
- Bieżący rekord dobowy bez zakończonej kontroli jakości jest pokazywany jako OPEN.
- Kolumny nowej tabeli można konfigurować w ostatniej karcie „Konfiguracja”.

## 0.21.1 — konfigurowalne i rozszerzone tabele

- Rozszerzono Planer o PV1/PV2, plan pompy ciepła, okna BUY/SELL, przepływy baterii, pełne decyzje PPD i przyczynę decyzji.
- Rozszerzono tabelę godzinową o SOC, liczbę slotów i czas aktualizacji.
- Rozszerzono tabelę dobową o baterię, eksport PV, pompę ciepła, CWU/CO, czasy produkcji oraz jakość i kompletność danych.
- Dodano edytor widoczności kolumn w karcie Konfiguracja, osobny dla każdego widoku tabeli.
- Wybór kolumn jest przechowywany lokalnie w przeglądarce i można go przywrócić do wartości domyślnych.
- Brak opcjonalnej encji bezpośredniej mocy baterii nie przerywa cyklu telemetrii po restarcie HA.

## 0.21.0 — pakiet offline do walidacji etapowej

- Dodano bezpieczny wybór źródła mocy baterii: dotychczasowe helpery pozostają domyślne, a `sensor.inverter_battery_power` można włączyć dopiero po potwierdzeniu znaku.
- PPD zachowuje złożoność O(n), uwzględnia konfigurowalną wartość końcowego SOC, wagę niepewności oraz bazowy cel około 60% o 20:00.
- Dodano trwałe ręczne override `AUTO/FORCE_ON/FORCE_OFF` dla sześciu procesów, z czasem ważności, przyczyną i historią.
- Dodano kontrakt komend z TTL, idempotencją intencji, wersją planu, źródłem decyzji i śladem bezpieczeństwa.
- Dodano potwierdzenia konektora `ACCEPTED/EXECUTED/REJECTED/FAILED`; wygasła komenda nie może zostać przyjęta ani wykonana.
- Executor pozostaje domyślnie wyłączony. Tryb techniczny jest domyślnie `DRY_RUN` i nie wywołuje usług Home Assistant.
- Zamknięcie slotu zapisuje plan, efektywny stan, obserwację i energię procesu; brak poboru EV jest oznaczany jako `IDLE_OR_DISCONNECTED`, a nie automatycznie jako błąd.
- Diagnostyka kontroluje wygasłe aktywne komendy, wielokrotne override oraz bramę executora.
- Alerty diagnostyczne tworzą deduplikowane po raporcie wpisy w trwałej tabeli TODO.
- Rozbudowano API o `overrides`, `commands`, `process-execution`, `todo`, zmianę override, staging komend i potwierdzenie konektora.
- Widok Procesy otrzymał responsywne sterowanie operatora dla sześciu procesów; ręczna cyrkulacja domyślnie trwa 45 minut.
- Dodano dokument etapowego odbioru. Pakiet nie jest przeznaczony do aktywacji executora przed ukończeniem bramek.

## 0.20.0

- Wykonanie zapisuje SOC początku, minimum, końca i zmianę SOC w slocie.
- Rzeczywista temperatura pochodzi z `sensor.klimat_w_ogrodzie_temperature`.
- Moce ogrzewania, CWU i chłodzenia HeishaMon są całkowane do energii kWh na slot.
- Zapisywane są energie pobrane/użytkowe oraz COP osobno dla trybów i łącznie.
- Dodano tryb HP, licznik uruchomień i godziny pracy sprężarki.
- Tabela Wykonanie pokazuje nowe pola SOC, temperatury i efektywności pompy.

## 0.19.0

- Telemetria odczytuje niezależne moce PV1 i PV2 bezpośrednio z falownika.
- Wykonanie zapisuje rzeczywistą energię PV1/PV2 w każdym slocie.
- Dodano temperatury zasilania i powrotu HP, delta T, częstotliwość i prąd sprężarki oraz przepływ.
- Slot zapisuje jednoznaczny status pracy pompy na podstawie częstotliwości sprężarki.
- Tabela Wykonanie pokazuje nowe pola PV i pompy; pola logiczne zachowują format `TAK/NIE`.
- Analityka liczy osobne WAPE dla PV1, PV2 i produkcji łącznej.

## 0.18.0

- Kompletny import 96 cen RCE automatycznie uruchamia jeden spójny cykl zależny.
- Po RCE odświeżane są prognozy PV i pogody, uzupełniane profile zużycia i publikowany Planer/PPD.
- Cykl RCE działa również podczas blokady zwykłych przeliczeń w godzinie 14:00.
- Ręczne odświeżenie RCE używa identycznego przebiegu i zwraca wynik publikacji planu.
- Niepełny import nie publikuje planu i jawnie zwraca `WAITING_FOR_96_RCE_ROWS`.

## 0.17.1

- Odtwarzanie po restarcie uzupełnia brakujące szczegóły Wykonania z telemetrii ostatnich 24 godzin.
- Migracja jest idempotentna i nie nadpisuje istniejących rekordów szczegółowych.
- Historyczne próbki CWU zapisane w kW są normalizowane podczas odtworzenia.

## 0.17.0

- Wykonanie zapisuje osobny, trwały rekord szczegółów każdego slotu.
- Dodano rzeczywistą energię EV i CWU, całkowity eksport sieciowy, liczbę próbek i procent pokrycia slotu.
- Eksport nie jest arbitralnie dzielony na PV/baterię; do czasu danych o trybie falownika ma status `UNRESOLVED`.
- Diagnostyka kontroluje średnie pokrycie telemetrii i liczbę slotów z pokryciem poniżej 80%.
- Tabela Wykonanie pokazuje nowe przepływy oraz jakość danych.
- Czujniki mocy są normalizowane do watów; obsługiwane są źródła raportujące W, kW i MW.
- Wszystkie widoki tabel mają jawny poziomy pasek przewijania.
- Binarne pola PPD są prezentowane jako `TAK` i `NIE`, bez zmiany zwykłych wartości liczbowych 0/1.
- Nagłówek tabeli i pierwsza kolumna `Slot` pozostają zablokowane podczas przewijania.
- Druga kolumna Planera i Wykonania pokazuje trwałą rekomendację planu.

## 0.16.0

- Konfigurację przeniesiono do ostatniej zakładki głównego panelu tabel.
- Parametry są grupowane w sekcje: Ceny i ekonomia, Bateria oraz Prognozy i procesy.
- Układ zakładki jest przygotowany do rozbudowy o kolejne grupy ustawień.

## 0.15.1

- Parametry operacyjne PPD usunięto z konfiguracji Supervisor po potwierdzeniu działania karty.
- Konfiguracja dodatku zawiera wyłącznie ustawienia techniczne; wartości PPD pozostają w trwałym pliku runtime.

## 0.15.0

- Panel zawiera kartę Parametry PPD z walidowanymi polami i przyciskiem Zastosuj.
- Parametry operacyjne są zapisywane trwale w `/data/runtime-settings.json`.
- Zmiany obowiązują od następnego przeliczenia bez restartu aplikacji.
- Każda aktualizacja parametrów jest zapisywana w dzienniku zdarzeń MariaDB.

## 0.14.1

- Po imporcie nowej doby RCE aplikacja natychmiast uzupełnia prognozę zużycia z profili.
- Endpoint RCE przyjmuje parametr `day=YYYY-MM-DD`, co umożliwia bezpieczne odświeżenie otwartych slotów bieżącej doby po zmianie marży.

## 0.14.0

- Harmonogram używa rzeczywistej minuty lokalnej zamiast minuty zaokrąglonego slotu.
- Naprawiono automatyczne uruchomienia planera, analityki i diagnostyki.
- Import następnej doby RCE jest ponawiany co 5 minut od 14:00 do 16:59 aż do uzyskania 96 rekordów.
- Po kompletnym imporcie RCE aplikacja odświeża Open‑Meteo przed uruchomieniem PPD.

## 0.13.0

- Pojemność, minimalny SOC, moc baterii, minimalną marżę i P80 przeniesiono do konfiguracji aplikacji.
- Progi PV→CWU i PV→EV są konfigurowalne; wartości startowe to 2,0 kW i 1,5 kW.
- CWU otrzymuje nadwyżkę po osiągnięciu dynamicznego celu SOC, bez wcześniejszego sztucznego wymogu 90%.
- EV zachowuje priorytet po CWU i rezerwę 20 punktów procentowych ponad cel SOC.

## 0.12.1

- Do konfiguracji dodano sprawność ładowania i rozładowania baterii.
- Do konfiguracji dodano koszt degradacji magazynu energii w PLN/kWh.
- PPD oraz okna ekonomiczne RCE nie zależą już od helperów V3 dla tych parametrów.

## 0.12.0

- Marża zakupu została przeniesiona do edytowalnej konfiguracji aplikacji.
- Domyślna wartość `purchase_margin_pln_kwh` wynosi 0,59 PLN/kWh.
- Import RCE nie zależy już od zerowego helpera pozostałego po V3.

## 0.11.1

- Źródło PV1/PV2 zmieniono na dedykowane encje Open‑Meteo.
- Prognoza temperatury, zachmurzenia i opadów pochodzi z godzinowej prognozy `weather.dom` (Open‑Meteo).
- Usunięto zależność prognoz aplikacji od Forecast.Solar.

## 0.11.0

- PPD zapisuje trwałą macierz sześciu procesów wraz z decyzją, przyczyną i ważnością.
- Dodano procesy BATTERY_IMPORT, BATTERY_EXPORT, PV_CWU, PV_EV, MANUAL_CIRCULATION i HP_DHW.
- Sprzedaż baterii jest wyznaczana niezależnie od starego planu V3 oraz blokowana poniżej podłogi SOC.
- Panel otrzymał widok Procesy; wszystkie decyzje pozostają niewykonywalne bez przyszłego konektora.

## 0.10.0

- Aplikacja tworzy sezonowe profile rozkładu produkcji PV z rzeczywistych wykonań.
- Dzienne prognozy Forecast.Solar PV1/PV2 są rozkładane na kwadranse z zachowaniem dokładnej sumy energii.
- Prognozy PV nie zależą już od ciągłego zapisu wykonywanego przez produkcyjny V3.

## 0.9.0

- Analityka tworzy 672 profile zużycia: dzień tygodnia × godzina × kwadrans.
- Profile przechowują średnią, średnią obciętą, P80, liczbę próbek i WAPE.
- Prognoza brakującego zużycia korzysta z profilu rzeczywistych wykonań po minimum trzech próbkach.

## 0.8.0

- Wykonanie: bezpośrednie próbkowanie dokładnej mocy ładowania i rozładowania baterii.
- Wykonanie: trwała energia ładowania/rozładowania w zamykanym slocie.
- Telemetria: dodano moc EV i CWU do danych źródłowych przyszłej analityki procesów.
- Odczyty Home Assistant są wykonywane równolegle, aby skrócić cykl próbkowania.

## 0.7.1

- Idempotentne odzyskiwanie przerwanych przebiegów planera i analityki po restarcie aplikacji.

## 0.7.0

- PPD: pełny, ciągły horyzont 96 slotów i wyłącznie ceny `PSE_API`.
- PPD: liniowy przebieg wsteczny dla przyszłych cen i ryzyka pogodowego.
- PPD: koszt odtworzenia energii uwzględnia sprawność, degradację i minimalną marżę.
- Analityka: trwałe przebiegi, jakość slotów i WAPE dla PV, zużycia, importu i eksportu.
- Diagnostyka: raporty świeżości telemetrii, kompletności planu, cen, wykonania i zablokowanych przebiegów.
- Panel: nowe widoki Analityka i Diagnostyka.

## 0.6.0 — 2026-09-09

- wdrożono aplikację Supervisor bez oznaczenia V3/V4;
- dodano własną ikonę i panel Ingress;
- utworzono odseparowaną bazę MariaDB `ems_gpt`;
- zmigrowano 50 tabel `ems_gpt_*` 1:1;
- odcięto konto aplikacji od bazy rekordera po migracji;
- przeniesiono zegar slotów, telemetrię i wznowienie po restarcie;
- przeniesiono etapowy planer oraz atomową publikację;
- przeniesiono dynamiczne SOC floor/target i polityki PPD;
- dodano bezpośredni import RCE PSE z dynamiczną marżą;
- dodano zamykanie wykonania, agregację godzinową i otwartą dobę;
- dodano profilowanie brakującego zużycia;
- wykonawca urządzeń pozostaje wyłączony do czasu wdrożenia konektora.

## Testy odbiorowe

- kompilacja Python: OK;
- instalacja i healthcheck: OK;
- MariaDB i odczyt HA: OK;
- migracja 50 tabel: OK;
- plan manualny: ACCEPTED, 54 otwarte sloty opublikowane;
- zapis wykonania: OK, slot zamknięty z 17 próbek;
- agregacja godzinowa: OK;
- agregacja dobowa: OK;
- restart wyłącznie aplikacji: OK, plan i baza zachowane;
- brak poleceń do urządzeń: potwierdzony.

