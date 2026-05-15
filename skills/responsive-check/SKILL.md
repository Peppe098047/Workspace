---
name: responsive-check
description: Use when a c14pipe page or component has mobile, tablet, breakpoint, overflow, spacing, grid, or small-screen layout issues, or when the user asks to verify or improve responsive behavior.
---

Il design system c14pipe ha due breakpoint definiti in `assets/css/main.css`:

## Breakpoint
- **900px** — tablet/mobile: navbar collassa, griglia diventa 2 colonne, menu hamburger visibile
- **480px** — mobile small: tutto a 1 colonna, font ridotti

## Cosa verificare a 900px
- `.nav-links`, `.search-form`, `.nav-divider` devono essere `display: none`
- `.mobile-toggle` deve essere visibile (`display: flex`)
- `.stat-grid` → `grid-template-columns: repeat(2, 1fr)`
- `.menu-grid` → `grid-template-columns: 1fr`
- `.catalog-grid` → `grid-template-columns: 1fr`
- `.page-hd` → `flex-direction: column`
- `.tbl-toolbar` → `flex-direction: column; align-items: flex-start`

## Cosa verificare a 480px
- `.main-content` → `padding: 1rem`
- `.stat-grid` → `grid-template-columns: 1fr`
- `.stat-value` → `font-size: 1.6rem`

## Errori comuni
- Usare `width` fisso in pixel invece di `%` o `minmax()`
- Tabelle senza `.tbl-wrap` (che gestisce `overflow-x: auto`)
- Testo con `white-space: nowrap` senza overflow gestito
- Grids con `grid-template-columns` fisso senza `auto-fit/auto-fill`
- Elementi con `position: absolute` che escono dal viewport su mobile

## Output
1. Lista dei problemi trovati con il breakpoint interessato
2. Il codice corretto
3. Suggerimento per testare: "Apri DevTools → Toggle Device Toolbar → seleziona 375px"
