---
name: css-check
description: Use when reviewing or fixing c14pipe HTML or PHP that contains inline styles, Bootstrap-style classes, hardcoded colors, non-standard CSS classes, or inconsistent visual patterns, and you want to map it back to the c14pipe design system.
---

Quando analizzi il codice per c14pipe, controlla:

## Cosa cercare
1. **Stili inline** (`style="..."`) — verifica se esiste una classe equivalente nel design system
2. **Classi Bootstrap** (es. `text-primary`, `btn btn-default`, `card`, `table`) — sostituiscile con le classi c14pipe
3. **Colori hardcoded** (es. `color: #fff`, `background: #333`) — sostituiscili con CSS variables
4. **Font hardcoded** — usa le classi `.mono` o `.mag` oppure le font-family tramite variabili

## Mappatura Bootstrap → c14pipe
- `btn btn-primary` → `.btn .btn-primary`
- `btn btn-secondary` / `btn btn-default` → `.btn .btn-outline`
- `btn btn-success` → `.btn .btn-success`
- `btn btn-danger` → `.btn .btn-danger`
- `table table-striped` → `.dtbl` dentro `.tbl-wrap`
- `badge bg-primary` → `.badge .badge-blue`
- `badge bg-success` → `.badge .badge-green`
- `badge bg-danger` → `.badge .badge-red`
- `badge bg-warning` → `.badge .badge-orange`
- `card` → `.catalog-card` o `.menu-card`
- `text-muted` → `.text-muted`
- `text-info` → `.text-info`
- `text-warning` → `.text-warning`
- `text-danger` → `.text-danger`
- `text-success` → `.text-success`

## Output
Fornisci:
1. Il codice originale con evidenziati i problemi
2. Il codice corretto con le classi del design system
3. Spiegazione delle sostituzioni effettuate
