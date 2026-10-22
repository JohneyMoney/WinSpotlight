# WinSpotlight

En Spotlight-lignende søkelinje for Windows. Trykk **Ctrl+Mellomrom** hvor som helst, skriv, og trykk Enter.

- Finner programmer (også Store-apper), filer og mapper
- Ekte app-ikoner og miniatyrbilder for bilder
- Løs søking: `vsc` finner Visual Studio Code, `kalk` finner Kalkulator
- Programmer du bruker ofte havner øverst
- Kalkulator rett i søkefeltet: `2^10*3`, `12,5*4`
- Åpne nettsider direkte (`vg.no`) eller søk på nettet
- Skriv `apper` for å se alle programmer alfabetisk
- Akryl-glass, avrundede hjørner og aksentfargen din, i lyst eller mørkt tema

## Hurtigtaster

| Tast | Handling |
|---|---|
| Ctrl+Mellomrom | Åpne / lukke |
| Enter | Åpne valgt treff |
| Ctrl+Enter | Åpne filplassering |
| Ctrl+Shift+C | Kopier filbane (eller lenke / svar) |
| ↑ ↓ / Tab | Flytt markering |
| Esc | Tøm søket / lukk |

## Installasjon

1. Last ned repoet (**Code → Download ZIP**) og pakk det ut.
2. Dobbeltklikk **`Installer.cmd`**.

Installasjonen:

- installerer Python med `winget` hvis det mangler
- installerer [pywebview](https://pywebview.flowrl.com/)
- kopierer appen til `%USERPROFILE%\WinSpotlight`
- slår på autostart

Hvis Windows viser «Windows beskyttet PC-en», trykk **Mer info → Kjør likevel**.

**Krav:** Windows 11, eller Windows 10 med Edge WebView2 Runtime.

### Autostart

Appen startes på flere uavhengige måter, så den kjører nesten alltid:

1. En planlagt oppgave ved innlogging
2. Den samme oppgaven når PC-en låses opp
3. Den samme oppgaven hvert minutt (gjør ingenting hvis appen allerede kjører)
4. Windows sin vanlige autostart-liste (`HKCU\...\Run`)

I tillegg starter en vaktprosess appen på nytt hvis den krasjer. Bare én kopi kjører om gangen.

### Avinstallering

Dobbeltklikk **`Avinstaller.cmd`**.

## Kjøre manuelt

```bash
pip install pywebview
pythonw spotlight.pyw
```

| Kommando | Hva den gjør |
|---|---|
| `python spotlight.pyw --autostart` | Slå på autostart |
| `python spotlight.pyw --no-autostart` | Slå av autostart |
| `python spotlight.pyw --demo "chr"` | Åpne vinduet med et søk ferdig utfylt |

Ikoner, historikk og feillogg lagres i `data/` ved siden av `spotlight.pyw`.

## Hvordan det er bygget

- **`spotlight.pyw`** inneholder:
  - global hurtigtast (`RegisterHotKey`)
  - indeksering av Start-menyen, `Get-StartApps` og brukermappene
  - rangering av søketreff
  - ikonuttrekk via `IShellItemImageFactory`
  - akryl via DWM
  - autostart
- **`ui.html`** er grensesnittet, som vises i et rammeløst WebView2-vindu via pywebview.

Vil du bruke en annen hurtigtast, endrer du `HOTKEY_MODS` og `HOTKEY_VK` øverst i `spotlight.pyw`.
