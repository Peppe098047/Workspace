# Report di sessione

In questa cartella il bot scrive **automaticamente un report markdown a ogni
arresto** (volontario o per crash): risultati, cambi di regime, ordini, eventi,
warning/errori e posizioni residue.

- `session_YYYY-MM-DD_HH-MM.md` — un file per ogni sessione, ordinabili per data.
- `LATEST.md` — copia sempre aggiornata dell'ultimo report.

## Per agenti AI (Claude, Codex, Gemini, …)

All'inizio di ogni sessione di lavoro su questo progetto:

1. Leggi `LATEST.md` (o il file più recente).
2. Parti dalla sezione **Anomalie**: shutdown da crash, conto non flat,
   errori ERROR/CRITICAL sono i segnali da investigare per primi.
3. Confronta gli eventi SKIP/REJECT con le strategie: se un titolo viene
   sistematicamente rifiutato o uno stop fallisce più volte, c'è qualcosa
   da correggere.
4. Aggiorna `AGENTS.md` con i bug trovati o i fix applicati.
