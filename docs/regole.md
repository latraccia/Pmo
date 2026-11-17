# Regole di sollecito

Le regole sono valutate da `rules/engine.py` dopo ogni sincronizzazione, con `pmo regole` e con il pulsante *Ricalcola adesso*. Soglie, gravità e attivazione si modificano da *Impostazioni → Regole*.

Vale solo per le pratiche **confermate** e non annullate/sospese. Per le pratiche **proposte dall'IA** vale solo la regola "Elementi da verificare".

| Codice | Nome | Condizione | Soglia | Gravità |
|---|---|---|---|---|
| `ordine_senza_specifiche` | Ordine senza specifiche | stato `ordine` da N giorni e nessun evento confermato di specifiche ricevute o inviate | 15 | avviso |
| `stima_in_attesa` | Stima non inviata | stato `stima` da N giorni senza stima o offerta inviata | 7 | avviso |
| `offerta_senza_risposta` | Offerta senza risposta | stato `offerta` da N giorni | 30 | avviso |
| `scadenza_vicina` | Scadenza vicina | scadenza entro N giorni | 7 | avviso |
| `scadenza_superata` | Scadenza superata | scadenza passata e pratica non chiusa | 0 | critico |
| `consegnato_non_fatturato` | Consegnato ma non fatturato | stato `consegna` da N giorni (una fattura emessa porta a `fatturazione`) | 30 | critico |
| `fatturato_diverso_da_ordinato` | Fatturato diverso dall'ordinato | pratica in `fatturazione`/`chiusa` con fatturato e ordinato che differiscono oltre la tolleranza (`tolleranza_percento`, `tolleranza_euro`) | - | avviso |
| `ordine_con_scostamenti` | Ordine diverso dall'offerta | un ordine confermato e non annullato ha esito "scostamenti" senza deroga. `tolleranza_percento` e `tolleranza_euro` sono anche la tolleranza usata dalla verifica degli ordini | - | avviso |
| `pratica_inattiva` | Pratica ferma | nessun evento da N giorni (riferimento: ultimo evento non di sistema, altrimenti data dello stato) | 30 | info |
| `da_verificare` | Elementi da verificare | pratica proposta o eventi proposti dall'IA creati da più di N giorni | 3 | info |

## Ciclo di vita di un sollecito

- **Aperto** quando la condizione compare. Chiave univoca `<pratica>:<regola>`: non si duplica mai.
- **In carico**: una persona lo ha preso; resta visibile.
- **Risolto**: manualmente o dal sistema quando la condizione cessa (`risolto_da = sistema`). Se la condizione ricompare viene **riaperto** e il contatore `occorrenze` aumenta.
- **Silenziato** fino a una data: non compare nel cruscotto né nella notifica email; alla scadenza dello snooze, se la condizione persiste, torna aperto.

La notifica email giornaliera (nel codice "digest") elenca i solleciti aperti e in carico, i nuovi in evidenza, con i contatori degli elementi da verificare. Arriva all'indirizzo email di ogni utente attivo che ha la notifica email attiva, scelta per utente in Impostazioni → Utenti e ruoli; non esiste un elenco di destinatari a parte. È personale: ognuno riceve le sole pratiche, mail e documenti che vede nell'applicazione (ruolo e team). `pmo digest --anteprima` mostra la notifica di ciascun destinatario.

## Aggiungere una regola

1. Aggiungere la specifica in `rules/definitions.py` (`REGOLE_DEFAULT`): viene creata al prossimo `pmo init-db` o alla prima esecuzione.
2. Aggiungere la valutazione in `valuta_pratica` (`rules/engine.py`) restituendo un `Esito` con titolo e messaggio in italiano.
3. Aggiungere un test in `tests/test_regole.py`.

## Verifica dell'ordine contro l'offerta

La verifica (`services/verifica_ordini.py`) non usa l'IA e si rifà a ogni modifica delle righe dell'ordine o dell'offerta.
L'ordine si confronta con la revisione d'offerta indicata sull'ordine o, se manca, con l'ultima revisione accettata della commessa.

1. **Abbinamento delle righe**: prima l'abbinamento scelto a mano nella pagina dell'ordine, poi lo stesso articolo del listino, poi stesso prodotto e modulo, poi stessa descrizione (senza maiuscole e punteggiatura). Un ordine creato dall'offerta (pagina dell'offerta, "Crea l'ordine da questa revisione", da una revisione inviata o accettata) riprende le righe con la quantità ancora da ordinare, i prezzi e le condizioni, e ogni riga nasce abbinata a mano alla sua riga d'offerta: si tolgono o si correggono solo le righe che il cliente non ha ordinato.
2. **Cliente e riferimenti**: il cliente dell'offerta deve essere quello della commessa dell'ordine; il numero d'offerta citato nell'ordine deve essere quello dell'offerta.
3. **Perimetro**: una riga senza corrispondenza è "non prevista dall'offerta"; una quantità oltre quanto resta della riga d'offerta, contando gli altri ordini confermati e non annullati, è uno scostamento.
4. **Importi**: il totale della riga si confronta con il prezzo netto offerto per la stessa quantità; la tolleranza è il maggiore tra `tolleranza_euro` e `tolleranza_percento` della regola `ordine_con_scostamenti`.
5. **Condizioni**: se offerta e ordine le indicano entrambe e sono diverse, è uno scostamento da verificare.

Esito: **conforme**, **scostamenti**, oppure **non determinabile** (manca l'offerta, mancano righe, quantità o prezzi). Un ordine è **parziale** quando restano righe d'offerta da ordinare: il residuo non autorizza attività.

**Deroga.** Responsabile, commerciale, direzione e amministratore possono accettare un ordine con scostamenti indicando il motivo; la cronologia registra autore e data. Se una verifica successiva dà un esito diverso, la deroga decade e la cronologia lo segnala.

**Esempio.** Offerta da 30.000 € (A 10.000, B 12.000, C 8.000), ordine da 18.000 € per A e C: conforme, parziale, residuo 12.000 € sulla riga B.

## Tipo della commessa e ordini di integrazione

Ogni commessa è una **fornitura** (licenze, sviluppo, hardware, avviamento) oppure una **manutenzione** (assistenza e canoni: di solito una commessa per periodo). Il tipo si sceglie alla creazione, si cambia nella modifica della commessa e si filtra nell'elenco; le commesse nate prima del tipo sono forniture.

Di regola una commessa ha **un ordine solo**: il primo registrato e non annullato è l'**ordine principale**. Un altro ordine sulla stessa commessa è ammesso (un ordine aggiuntivo, una tranche della stessa offerta) ma è segnato come **integrazione**, nella commessa, nel portafoglio ordini e nella sua pagina, e alla creazione compare un avviso. Se il principale viene annullato, il successivo prende il suo posto. La verifica contro l'offerta e il residuo valgono per tutti gli ordini insieme, come prima.

## Righe lette dai documenti

Quando un evento **ordine ricevuto**, **offerta inviata** od **offerta accettata** viene confermato e viene da una mail con allegati PDF o Word, o da un documento Drive, il documento si mette in coda per la lettura. La lettura avviene al giro di sincronizzazione successivo (`services/estrazioni.py`, `ingest/estrazione_righe.py`, `ingest/estrattore.py`).

- **Una sola offerta per numero.** Le offerte si cercano per numero, senza badare a spazi e segni, in tutte le commesse: la stessa offerta letta dalla mail d'invio, dall'archivio Drive o dal protocollo è una sola offerta. Senza numero vale l'unica offerta della commessa. Gli eventi non creano revisioni: l'ultima diventa *inviata*, o *accettata* con un'offerta accettata. Se il documento indica una revisione ("rev. 2"), le righe vanno su quella revisione.
- **Righe proposte.** Le righe lette sostituiscono la riga "Da dettagliare" e le altre righe proposte, mai quelle scritte o confermate. Se la destinazione ha già righe confermate, il documento non si legge. Le righe proposte contano nei totali e nella verifica; si confermano tutte insieme, oppure una per una salvandole.
- **Testata.** Numero, offerta citata e condizioni si riempiono solo se sono vuoti. La data del documento sostituisce quella della mail per ordini e offerte creati da un evento; per quelli scritti a mano si usa solo se manca.
- **Listino.** Una riga con un codice del listino si abbina all'articolo, tenendo descrizione e prezzo del documento. Una riga con un codice nuovo propone "Aggiungi al listino"; una con un codice o una descrizione già presenti propone "Abbina a…".
- **Controlli.** Quantità × prezzo − sconto deve dare l'importo della riga (tolleranza 5 centesimi); la somma delle righe deve dare il totale del documento (tolleranza mezzo punto percentuale); ogni riga deve avere un prezzo. Le differenze compaiono come avvisi sulla lettura. Un documento che non è un ordine o un'offerta non viene applicato.
- **Niente doppie letture.** Lo stesso file (stesso hash) arrivato da un'altra fonte non va a Claude; un file diverso della stessa offerta, quando le righe sono già lette, viene solo registrato come fonte in più.
- **Eventi scartati.** Scartare o eliminare l'evento elimina l'ordine o l'offerta che aveva creato, se ha solo righe "Da dettagliare" o proposte; con righe confermate resta, staccato dall'evento.

