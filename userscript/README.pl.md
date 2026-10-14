# Przycisk StashCast na YouTube

*[English version: README.md](README.md)*

Skrypt (userscript), który dodaje do YouTube przycisk StashCast. Klikasz go przy filmie,
a StashCast go pobiera – bez kopiowania linków i bez formularza w panelu.

- Okrągły przycisk **+** jest na każdej miniaturce filmu: strona główna, wyszukiwarka,
  filmy kanału, proponowane, Shorts.
- Duży przycisk **+** wisi w prawym dolnym rogu, gdy oglądasz film.
- Jedno kliknięcie wysyła film do StashCast. Przycisk zmienia się w **✓**, a krótki
  komunikat mówi, gdzie film trafił.

## Gdzie trafia film

Decyduje o tym StashCast, nie skrypt:

1. **Kanał filmu jest przypisany do którejś z Twoich grup** (pole *Adres kanału YouTube*
   w grupie): film trafia do tej grupy, z jej ustawieniami – audio czy wideo,
   transkrypcja, powiadomienia. Dopasowanie działa niezależnie od tego, czy w grupie
   wpisano `@nazwa`, czy `/channel/UC…`.
2. **Każdy inny kanał:** film trafia do **grupy domyślnej** ustawionej w skrypcie.
3. **StashCast już ma ten film** (pobrany, w kolejce albo właśnie się pobiera): nic nie
   jest pobierane ponownie; przycisk pokazuje **✓** i „Już jest w StashCast”.

Kliknięcie filmu, który czeka na zatwierdzenie albo został wcześniej odrzucony, pobiera
go – kliknięcie jest Twoim zatwierdzeniem.

## Czego potrzebujesz

- StashCast dostępnego z Maca / iPhone'a przez **HTTPS**.
- Darmowej aplikacji **[Userscripts](https://apps.apple.com/app/userscripts/id1463298887)**
  (open source, autor: quoid) – na macOS i iOS. To rozszerzenie Safari, które uruchamia
  skrypty takie jak ten, więc nie potrzebujesz Xcode, konta deweloperskiego Apple ani
  żadnego podpisywania.
- Swojego `STASHCAST_USER_TOKEN` – strona opisana niżej wpisze go za Ciebie.

## Skąd wziąć skrypt

W panelu StashCast otwórz **Narzędzia → Przycisk YouTube**.

1. Wybierz **Grupę domyślną** (i typ pobierania – zwykle wystarczy „Ustawienie grupy”).
2. Kliknij **Zaktualizuj skrypt**.
3. Użyj **Zainstaluj / pobierz stashcast.user.js** albo **Kopiuj skrypt**.

Skrypt na tej stronie ma już wpisany adres serwera i token. Plik w tym folderze
(`stashcast.user.js`) zawiera zamiast nich przykładowe wartości – jeśli używasz go
bezpośrednio, popraw blok konfiguracji na początku:

```js
const STASHCAST_URL = 'https://stashcast.example.com';
const TOKEN = 'paste-your-STASHCAST_USER_TOKEN-here';
const DEFAULT_GROUP = '';   // slug grupy, np. 'inbox'; '' = bez grupy
const TYPE = 'auto';        // auto | audio | video
```

## macOS

1. Zainstaluj **Userscripts** z Mac App Store i uruchom aplikację raz.
2. Safari → **Ustawienia → Rozszerzenia**: zaznacz **Userscripts**. W dostępie do stron
   zezwól na **youtube.com** (albo „Wszystkie witryny”).
3. Zainstaluj skrypt, na jeden z dwóch sposobów:
   - otwórz w Safari link **Zainstaluj / pobierz stashcast.user.js** z panelu StashCast,
     kliknij ikonę Userscripts na pasku narzędzi i wybierz **Install**; albo
   - kliknij ikonę Userscripts → **Open Extension Page** → **+** → **New JS**, wklej
     skopiowany skrypt i zapisz (⌘S).
4. Otwórz lub odśwież youtube.com – przyciski **+** pojawią się na miniaturkach.

## iOS / iPadOS

1. Zainstaluj **Userscripts** z App Store i otwórz aplikację.
2. Stuknij **Set Userscripts Directory** i wybierz folder, np. *iCloud Drive → Userscripts*.
3. Ustawienia → **Safari → Rozszerzenia → Userscripts**: włącz je i zezwól na
   **youtube.com** (albo „Wszystkie witryny”).
4. Zainstaluj skrypt, na jeden z dwóch sposobów:
   - w Safari zaloguj się do StashCast, otwórz **Narzędzia → Przycisk YouTube →
     Zainstaluj / pobierz stashcast.user.js**, potem stuknij ikonę rozszerzeń w pasku
     adresu → **Userscripts** → **Install**; albo
   - zapisz plik `stashcast.user.js` w folderze z kroku 2 przez aplikację Pliki.
5. Otwórz youtube.com **w Safari** – przyciski pojawią się na miniaturkach i przy filmie.

Skrypt działa tylko w Safari. Dla **aplikacji YouTube** użyj skrótu opisanego niżej.

## Skrót iOS dla aplikacji YouTube (opcjonalnie)

Aplikacja YouTube nie uruchamia skryptów, ale jej menu **Udostępnij** może uruchomić
Skrót, który wyśle link do StashCast – z tą samą logiką grup.

1. Otwórz **Skróty** → **+** → nazwij skrót *StashCast*.
2. Stuknij **ⓘ** → włącz **Pokazuj w arkuszu udostępniania**; ustaw, że przyjmuje
   **adresy URL**.
3. Dodaj czynność **Pobierz zawartość URL**:
   - URL: `https://adres-twojego-stashcast/stash/`
   - Metoda: **POST**, Treść żądania: **Formularz**, z polami:

     | Klucz | Wartość |
     |---|---|
     | `token` | Twój `STASHCAST_USER_TOKEN` |
     | `url` | *Dane wejściowe skrótu* |
     | `type` | `auto` |
     | `group` | slug grupy domyślnej (pomiń, jeśli bez grupy) |
     | `skip_existing` | `true` |

4. Dodaj **Pobierz wartość słownika** → klucz `group`, z *Zawartości URL*.
5. Dodaj **Pokaż powiadomienie** → „StashCast: *Wartość słownika*”.

W aplikacji YouTube: **Udostępnij → StashCast**. Powiadomienie pokaże nazwę grupy, do
której trafił film; pusta nazwa oznacza, że trafił bez grupy.

## Rozwiązywanie problemów

| Co widzisz | Co zrobić |
|---|---|
| Brak przycisków na YouTube | Userscripts nie ma zgody na youtube.com albo skrypt jest wyłączony w Userscripts. Po zmianie odśwież stronę. |
| Przycisk jest przy oglądanym filmie, ale nie ma go na miniaturkach | YouTube zmienił układ strony. Zaktualizuj skrypt z tego folderu; przyciski trafiają na linki do `/watch?v=` zawierające obrazek. |
| `StashCast: Invalid or missing user token` | Token w skrypcie jest niepoprawny – pobierz skrypt ponownie ze strony w panelu. |
| `StashCast: Unknown group: …` | Grupę domyślną przemianowano lub usunięto – wybierz ją ponownie na stronie w panelu. |
| `Brak połączenia ze StashCast` | Sprawdź `STASHCAST_URL` (musi być HTTPS i dostępny z tego urządzenia). |

Przycisk czeka, aż StashCast sprawdzi film na YouTube – kilka sekund – zanim zmieni się
w ✓.

## Bezpieczeństwo

Skrypt zawiera Twój token: każdy, kto go ma, może dodawać pobrania do Twojego StashCast.
Trzymaj go tylko na swoich urządzeniach. Jeśli wycieknie, zmień `STASHCAST_USER_TOKEN`
i pobierz skrypt ponownie.

## Dla programistów: endpoint

Skrypt wywołuje `POST /stash/` z polami formularza:

| Pole | Znaczenie |
|---|---|
| `token` | `STASHCAST_USER_TOKEN` (wymagane) |
| `url` | adres filmu (wymagane); skrypt wysyła `https://www.youtube.com/watch?v=ID` |
| `type` | `auto`, `audio` albo `video` (wymagane); `auto` bierze typ z ustawień grupy |
| `group` | grupa domyślna, po slugu albo nazwie (opcjonalne) |
| `skip_existing` | `true`: zamiast pobierać film, który StashCast już ma, zwraca `"already": true` |

Odpowiedź JSON zawiera `group`, `matched_channel` (true, gdy użyto grupy kanału),
`already` i `detail_url`.
