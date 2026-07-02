# La strategia di regime-trader, spiegata semplice

*Documento divulgativo — aggiornato all'11 giugno 2026.*

---

## 1. Cosa fa questo bot, in una frase

È un programma che ogni giorno compra un piccolo gruppo di azioni e fondi
americani molto scambiati, **investendo di più quando il mercato è calmo e di
meno quando è agitato**, proteggendo ogni acquisto con un ordine automatico di
vendita in caso di perdita, e chiudendo **tutto** ogni sera prima della
chiusura della Borsa.

Non prevede il futuro. Non sa se domani si sale o si scende. Misura *che tipo
di giornata è* e adatta la prudenza di conseguenza.

---

## 2. L'idea di fondo: il "meteo" del mercato

### La volatilità

La **volatilità** è quanto i prezzi "ballano": un mercato dove l'indice si
muove dello 0,3% al giorno è calmo; uno dove si muove del 3% è una tempesta.
La volatilità non dice se si sale o si scende — dice quanto è **agitato** il
mare.

Il punto chiave su cui si fonda tutto: **prevedere la direzione dei prezzi è
quasi impossibile, ma riconoscere l'agitazione del mercato è fattibile**. La
volatilità tende a fare "grappoli": le giornate tranquille seguono giornate
tranquille, le tempeste seguono tempeste. È molto più persistente della
direzione.

### Il modello HMM — il termometro

Per classificare il clima usiamo un **HMM (Hidden Markov Model, "modello di
Markov nascosto")**. Sembra complicato, ma l'idea è questa: immaginate che il
mercato abbia degli "umori" che non si possono osservare direttamente (da qui
"nascosto") — sereno, nervoso, in panico. Quello che si osserva sono solo gli
effetti: i movimenti dei prezzi, ora per ora. L'HMM è una tecnica statistica
che, guardando gli effetti, risale all'umore più probabile **in questo
momento**.

Il nostro HMM guarda l'andamento orario dell'indice S&P 500 (tramite l'ETF
SPY, il "termometro" del mercato USA) e classifica il momento in regimi di
volatilità: **bassa, media o alta**. Due accorgimenti importanti:

- Usa solo i dati **fino a ora** — mai informazioni dal futuro (in gergo:
  niente *look-ahead bias*, l'errore di chi testa una strategia facendole
  "vedere" dati che nella realtà non avrebbe avuto).
- Ha dei filtri anti-indecisione: un cambio di regime va **confermato** per
  più ore di fila prima di agire, e se il modello cambia idea troppo spesso
  (in gergo: *flicker*) il bot dimezza automaticamente le dimensioni delle
  posizioni per prudenza.

### Cosa fa in ogni regime

| Clima | Quanto investe | Logica |
|---|---|---|
| Volatilità bassa | fino all'80% del capitale | i mercati calmi tendono a salire piano |
| Volatilità media | 60–80% a seconda del trend | prudenza se il trend si rompe |
| Volatilità alta / crash | massimo 60% in totale | si riduce, non si scappa |

Il principio forse controintuitivo: **anche in un crash il bot non scommette
mai al ribasso sulle singole azioni**. Riduce l'esposizione e basta. Il motivo
è che i rimbalzi dal fondo ("rimbalzi a V") sono violenti e arrivano senza
preavviso: chi scommette al ribasso in ritardo viene travolto.

---

## 3. Quali titoli compra: l'imbuto a tre stadi

### Stadio 1 — L'universo (58 strumenti)

Una lista fissa di grandi aziende americane (Apple, Microsoft, NVIDIA,
JPMorgan, Coca-Cola…) e di ETF sugli indici. Un **ETF** è un fondo quotato in
Borsa che si compra come un'azione: comprando SPY si compra "un pezzettino di
tutto l'S&P 500".

Nell'universo ci sono anche 4 **ETF inversi**: fondi costruiti per **salire
quando l'indice scende** (SH è l'inverso dell'S&P 500, DOG del Dow Jones,
ecc.). Sono il modo per guadagnare dai ribassi *comprando* qualcosa — senza
le complicazioni e i rischi della vendita allo scoperto (vedi sezione 7).

### Stadio 2 — Lo screener di liquidità (da 58 a 12)

Ogni mattina il bot misura la **liquidità** di ogni titolo: quanti dollari di
scambi fa al giorno. Perché conta? Per via dello **spread**: la differenza tra
il prezzo a cui si può comprare e quello a cui si può vendere in un dato
istante. È il vero "costo del biglietto" di ogni operazione (le commissioni
sono zero sul nostro broker). Un titolo molto scambiato ha spread quasi nullo;
uno poco scambiato può costare 10 volte tanto a ogni ingresso e uscita.

Per un bot che opera ogni giorno, lo spread è l'affitto di casa: va
minimizzato. Quindi: solo i 12 titoli più liquidi del giorno (di cui 4
garantiti: SPY e i 3 inversi).

### Stadio 3 — Il ranking momentum (da 12 a 5)

Il **momentum** è una delle regolarità più documentate in finanza: i titoli
che si sono comportati meglio di recente tendono, in media, a continuare nel
breve periodo. Ogni ora il bot dà un punteggio a ciascuno dei 12:

- **rendimento recente diviso per la volatilità del titolo**: +3% su un titolo
  tranquillo vale più di +3% su uno che oscilla come un ottovolante;
- **forza relativa**: sta battendo il mercato (SPY), o sale solo perché sale
  tutto?

Solo i migliori **5** possono essere comprati. Nota elegante: nei giorni di
ribasso gli ETF inversi salgono in classifica *da soli* — il bot "cambia lato"
senza che nessuno glielo ordini. Una regola di **mutua esclusione** impedisce
di avere in portafoglio un indice e il suo inverso insieme (sarebbe come
scommettere su entrambe le squadre pagando due volte il bookmaker).

---

## 4. Quanto compra: le regole di dimensionamento

- **Mai più del 15% del capitale su un singolo titolo.**
- **Mai più di 5 posizioni contemporaneamente.**
- **Tetto totale legato al regime**: in alta volatilità il portafoglio intero
  non supera il 60% del capitale; il resto sta in liquidità.
- **Niente leva e niente margine**: si investono solo i soldi che ci sono.
  La **leva** è investire denaro preso a prestito dal broker: moltiplica i
  guadagni ma anche le perdite, e può far perdere più di quanto si ha. Qui è
  vietata per costruzione (`leva massima = 1.0`).

---

## 5. Come si protegge: la gestione del rischio

### Lo stop loss

Ogni acquisto nasce con uno **stop loss**: un ordine automatico, registrato
direttamente sul broker, che vende il titolo se il prezzo scende sotto una
soglia. È la cintura di sicurezza: limita la perdita massima di ogni singola
operazione, e funziona anche se il bot si spegnesse.

La distanza dello stop non è fissa: si adatta alla volatilità del titolo
tramite l'**ATR (Average True Range)** — una misura di quanto un titolo si
muove "normalmente" in un'ora. Un titolo nervoso ha bisogno di uno stop più
largo (altrimenti verrebbe venduto dal semplice rumore), ma in compenso il bot
ne compra meno pezzi: il **rischio in euro resta uguale** (mai più dell'1% del
capitale a rischio per singola operazione).

### Il trailing stop

Se il titolo sale, lo stop **sale con lui** (mai il contrario): è il *trailing
stop*, che trasforma progressivamente una posizione in profitto in una
posizione che non può più diventare una perdita. Viene ricalcolato a ogni
barra oraria e aggiornato coi prezzi correnti ogni 5 minuti.

### Le difese di squadra

- **Risk manager con potere di veto**: ogni ordine, prima di partire, passa da
  un controllore indipendente che verifica limiti di esposizione, rischio per
  operazione, presenza dello stop. Se qualcosa non torna, l'ordine muore lì.
- **Circuit breaker**: se le perdite di giornata superano il 3%, il bot smette
  di operare per quel giorno. Se il conto scende del 10% dal suo massimo
  storico, si ferma tutto finché un umano non decide.
- **Cooldown anti-accanimento**: dopo che uno stop ha chiuso una posizione, il
  bot non può ricomprare lo stesso titolo per 30 minuti (evita il tira e
  molla nelle giornate storte).

---

## 6. Il ritmo della giornata (orari italiani)

| Ora | Cosa succede |
|---|---|
| 15:30 | apre la Borsa USA. Il bot osserva ma **non compra**: la prima mezz'ora ha spread larghi e movimenti falsi |
| 16:00 | primo ciclo di decisioni; poi **un ciclo ogni ora** alla chiusura di ogni barra oraria |
| ogni 5 min | manutenzione: aggiornamento dei trailing stop coi prezzi correnti |
| eventi macro | nei 15 minuti prima e dopo i grandi annunci economici USA (inflazione, occupazione, decisioni della Fed) **niente acquisti**: il mercato in quei momenti è una lotteria |
| 21:45 | **square-off**: il bot vende tutto, qualunque cosa abbia in mano |
| 22:00 | chiude la Borsa. Il conto è in contanti, il bot dorme |

Perché vendere tutto ogni sera? Per eliminare il **gap risk**: a mercato
chiuso possono uscire notizie (trimestrali, geopolitica) e il mattino dopo un
titolo può riaprire il 10% più in basso **saltando** lo stop loss, che non può
proteggerti mentre la Borsa è chiusa. Niente posizioni di notte = niente
brutte sorprese al risveglio.

---

## 7. Cosa NON fa, e perché (le scelte più importanti)

- **Non vende allo scoperto** (short): scommettere sul ribasso vendendo titoli
  presi a prestito richiede margine, espone a perdite teoricamente illimitate
  (un titolo può raddoppiare, e tu paghi la differenza) e si fa massacrare dai
  rimbalzi. L'esposizione al ribasso si ottiene comprando gli ETF inversi.
- **Non usa la leva**: solo capitale proprio.
- **Non ha take profit** (l'ordine che vende automaticamente a un obiettivo di
  guadagno): preferisce lasciar correre i profitti col trailing stop, che
  insegue il prezzo invece di tagliare le ali a un buon trade.
- **Non legge le notizie per decidere**: usa il calendario economico solo per
  *non* operare nei momenti pericolosi, mai per "puntare" su un annuncio.
- **Non promette l'impossibile**: in un mercato che crolla, un sistema che
  compra soltanto perde — per costruzione. L'obiettivo nei crash è perdere
  POCO e in modo controllato, e farsi trovare investito quando si riparte.

---

## 8. Come sappiamo se funziona

- **Paper trading**: il bot opera su un conto di simulazione con denaro
  virtuale ma prezzi, ordini e meccanismi reali. Nessun euro vero finché i
  numeri non lo giustificano.
- **Backtest walk-forward**: la strategia è stata provata sui dati storici col
  metodo più onesto — il modello viene addestrato su un periodo e testato sul
  periodo *successivo*, mai visto prima, a scorrere. Con i costi inclusi.
- **Report e journal automatici**: ogni sessione produce un report completo
  (operazioni, errori, risultati) e un registro cumulativo con le statistiche
  di tutte le sessioni. Le decisioni di modifica si prendono su quei numeri,
  non sulle sensazioni dell'ultima giornata.

---

## Mini-glossario

| Termine | Significato |
|---|---|
| **ATR** | misura di quanto un titolo oscilla "normalmente" in un intervallo di tempo |
| **ETF** | fondo quotato che replica un indice; si compra come un'azione |
| **ETF inverso** | ETF che sale quando il suo indice scende |
| **Gap risk** | rischio che un titolo riapra a un prezzo molto diverso dalla chiusura, saltando gli stop |
| **HMM** | modello statistico che deduce lo "stato nascosto" (il regime) dai movimenti osservati |
| **Leva / margine** | operare con denaro preso a prestito dal broker — qui vietato |
| **Liquidità** | quanto un titolo è scambiato; più liquidità = costi di negoziazione più bassi |
| **Momentum** | tendenza dei titoli più forti di recente a restare forti nel breve |
| **Paper trading** | trading simulato con denaro virtuale su prezzi reali |
| **Regime** | il "clima" del mercato: volatilità bassa, media o alta |
| **Short** | vendita allo scoperto: scommessa al ribasso con titoli in prestito — qui vietata |
| **Spread** | differenza tra prezzo di acquisto e di vendita in un istante: il costo reale di ogni trade |
| **Square-off** | chiusura di tutte le posizioni a fine giornata |
| **Stop loss** | ordine automatico che vende se il prezzo scende sotto una soglia |
| **Trailing stop** | stop loss che sale insieme al prezzo, bloccando i profitti |
| **Volatilità** | quanto i prezzi si muovono, a prescindere dalla direzione |

---

*Generato per scopi divulgativi. Non è un consiglio di investimento: è la
descrizione di un esperimento in corso su un conto di simulazione.*
