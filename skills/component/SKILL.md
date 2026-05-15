---
name: component
description: Use when working on the c14pipe frontend and the user asks to create or update a UI component such as a card, table, badge, button, page section, toolbar, or reusable HTML or PHP fragment that should follow the existing c14pipe design system.
---

Quando generi un componente per c14pipe, segui sempre queste regole:

## Design System
- Usa **esclusivamente** le classi CSS definite in `assets/css/main.css` — mai Bootstrap, Tailwind o altre librerie
- Usa le CSS variables definite in `:root` (es. `var(--bg-card)`, `var(--accent-blue)`, `var(--border)`)
- Non aggiungere mai stili inline se esiste già una classe nel design system

## Classi disponibili
- Layout: `.stat-grid`, `.menu-grid`, `.catalog-grid`
- Cards: `.stat-card`, `.menu-card`, `.catalog-card`
- Tabelle: `.tbl-wrap`, `.dtbl`, `.tbl-toolbar`, `.tbl-search`
- Badges: `.badge`, `.badge-blue`, `.badge-green`, `.badge-red`, `.badge-purple`, `.badge-gold`, `.badge-orange`, `.badge-gray`
- Bottoni: `.btn`, `.btn-primary`, `.btn-outline`, `.btn-success`, `.btn-danger`, `.btn-sm`, `.btn-icon`
- Chips: `.chip`, `.chip.active`
- Link analisi: `.alink`, `.alink-g`, `.alink-s`, `.alink-p`, `.alink-2`, `.alink-cs`, `.alink-v`, `.alink-a`
- Testo: `.mono` (JetBrains Mono), `.mag` (magnitudine in oro), `.text-info`, `.text-warning`, `.text-danger`, `.text-success`, `.text-muted`
- Struttura pagina: `.page-hd`, `.page-title`, `.page-sub`, `.page-actions`

## Tipografia
- Titoli: `Space Grotesk`
- Testo: `Inter`
- Codice/coordinate/numeri: `JetBrains Mono` tramite classe `.mono`

## Tema
- Sfondo dark astronomico, colori accent: blue `#58a6ff`, gold `#e3b341`, green `#3fb950`, red `#f85149`, purple `#bc8cff`
- Hover sempre con `transition: var(--transition)`

## Output
Fornisci sempre:
1. Il codice HTML/PHP del componente
2. Una breve spiegazione delle classi usate
3. Eventuali note su come integrarlo nelle pagine esistenti
