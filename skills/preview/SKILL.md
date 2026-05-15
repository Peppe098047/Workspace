---
name: preview
description: Use when working on c14pipe and the user asks to update, regenerate, extend, or demo the static preview page, especially when they want to add a component to preview.html or inspect UI output without running the PHP app.
---

Il file `Project/preview.html` è una pagina HTML statica che mostra il design system di c14pipe senza bisogno di PHP o database.

## Struttura del file
- Include `assets/css/main.css` e `assets/js/app.js` via path relativi
- Ha un banner giallo fisso che identifica la pagina come preview statica
- È divisa in sezioni con etichette `.section-label`
- Usa dati fake per simulare l'output delle pagine PHP

## Quando aggiorni preview.html
1. Leggi prima il contenuto attuale del file
2. Aggiungi la nuova sezione **in fondo**, prima del footer
3. Usa sempre una `.section-label` per identificare la sezione
4. Usa dati fake realistici (nomi di galassie, coordinate astronomiche, date plausibili)
5. Mantieni il JS funzionante (DataGrid, toast, chip filter)

## Dati fake realistici per astronomia
- Nomi oggetti: NGC 1275, M31, IC 342, NGC 4258, M82, M51, NGC 891, NGC 5194
- Coordinate RA: `03h19m48s`, `00h42m44s`, `12h18m57s`
- Coordinate DEC: `+41°30′42″`, `+41°16′09″`, `+47°18′14″`
- Filtri: Green, Red, IR, Z
- Date: 2025-03-15, 2025-03-14, ecc.
- Magnitudini: valori tra 10.5 e 18.9

## Dopo l'aggiornamento
Apri sempre il file con:
```
xdg-open /home/giuseppe/Fabrizio/c14pipe/Project/preview.html
```
