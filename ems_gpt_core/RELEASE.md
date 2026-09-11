# EMS-GPT Core 0.25.16

## Zmiana

Naprawiono wywołanie Home Assistant `weather.get_forecasts`. Aplikacja żąda teraz odpowiedzi usługi przez `return_response`, dzięki czemu prognoza godzinowa jest poprawnie odbierana zamiast błędu HTTP 400.

## Bezpieczeństwo

Wykonawca pozostaje domyślnie OFF, dry-run ON, a wywołania skryptów sterujących nie korzystają z parametru odpowiedzi.
