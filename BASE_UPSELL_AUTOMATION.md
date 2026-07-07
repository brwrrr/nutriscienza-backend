# Upsell automatico Base → Completo

**Cosa fa:** invia automaticamente un'email a ogni acquirente del **Piano Base (€19)**
circa **un mese dopo l'acquisto**, proponendo l'upgrade al **Piano Completo** (che
ricalcola il piano ogni mese) con un'offerta "primo mese a €24 invece di €29".

Implementato il 2026-07-07. Nessuna nuova infrastruttura: gira sullo **sweep giornaliero
esistente** (`scheduler.run()`), lo stesso già invocato dal cron Render via
`POST /api/admin/run-refresh`.

---

## Come funziona

Ogni giorno lo sweep controlla gli ordini Base pagati e seleziona quelli acquistati
**tra 30 e 45 giorni fa** che non hanno ancora ricevuto l'email. Per ognuno invia
l'upsell (via Resend) e marca l'ordine come contattato, così **non viene mai
ricontattato due volte**.

La finestra 30–45 giorni (non un istante secco) garantisce che, anche se un run del
cron salta un giorno, nessun cliente venga perso — e la marcatura anti-doppione
impedisce invii ripetuti.

### Chi riceve l'email (e chi NO)
Vengono inclusi solo gli ordini che soddisfano **tutti** questi criteri:
- piano = `base` e stato pagato (`paid` / `generating` / `sent`);
- acquisto tra 30 e 45 giorni fa;
- upsell non ancora inviato;
- ordine non anonimizzato (GDPR);
- l'email **non** appartiene a un abbonato attivo → chi ha già fatto upgrade non
  viene infastidito.

> Scelta di lancio: **niente blast retroattivo**. Chi ha comprato il Base più di 45
> giorni fa non riceve l'email. Partono solo i clienti nella finestra 30–45 giorni e
> tutti i nuovi acquirenti Base d'ora in avanti. Più sicuro per la reputazione di invio.

---

## ⚠️ Unico passo manuale: creare il codice sconto su Stripe

Per far funzionare davvero l'offerta "€24 invece di €29":

1. Stripe Dashboard → **Products → Coupons → New** → es. **€5 off**, durata *"Once"*
   (si applica solo alla prima fattura).
2. Crea una **Promotion Code** collegata a quel coupon, es. codice `RICALCOLO24`
   (consigliato: *Limit to first-time / once per customer*).
3. Imposta la variabile d'ambiente su Render:
   ```
   base_upsell_promo_code=RICALCOLO24
   ```

Il checkout ha già `allow_promotion_codes=True`, quindi il cliente digita il codice
nella pagina di pagamento Stripe.

**Se `base_upsell_promo_code` è vuoto** (default): l'email viene comunque inviata, ma
**senza** il prezzo scontato — propone il Completo a prezzo pieno con una CTA neutra.
Così il sistema è sicuro anche prima che tu crei il codice (nessun cliente vede un
codice "non valido").

---

## Configurazione (variabili d'ambiente)

| Variabile | Default | Descrizione |
|---|---|---|
| `base_upsell_promo_code` | `""` | Codice promo Stripe da mostrare (vuoto = nessuno sconto) |
| `base_upsell_offer_price` | `24` | Prezzo scontato primo mese (solo testo email) |
| `base_upsell_full_price` | `29` | Prezzo pieno Completo (solo testo email) |
| `base_upsell_min_days` | `30` | Giorni minimi dall'acquisto prima dell'invio |
| `base_upsell_max_days` | `45` | Limite massimo della finestra di invio |

---

## File modificati

- **`app/config.py`** — nuovi setting `base_upsell_*`.
- **`app/storage.py`** —
  - migrazione: nuova colonna `orders.base_upsell_sent_at` (auto-applicata all'avvio);
  - `get_base_orders_due_for_upsell(min_days, max_days)` — selezione idonei;
  - `mark_base_upsell_sent(order_id)` — marca l'invio **senza toccare `updated_at`**
    (importante: `updated_at` guida i totali di fatturato per periodo; spostarla farebbe
    riapparire un ordine vecchio nel giro d'affari "di oggi").
  - aggiunto `timedelta` agli import.
- **`app/email_sender.py`** — `send_base_upsell_email(email, first_name)` + template
  HTML brandizzato "il tuo piano ha una data di scadenza" (mostra il blocco offerta
  solo se il codice promo è configurato).
- **`app/scheduler.py`** — `_send_base_upsells()` + chiamata dentro `run()`.

## Deploy

1. Merge del branch backend.
2. Deploy su Render — la migrazione della colonna parte da sola al primo avvio
   (`init_db()`), idempotente.
3. (Consigliato) crea il codice promo Stripe e imposta `base_upsell_promo_code`.
4. Nessun nuovo cron da creare: se il tuo cron giornaliero chiama già
   `/api/admin/run-refresh` (come per i rinnovi), l'upsell viaggia su quello.

## Test

Verificato con un DB SQLite temporaneo: la query seleziona **solo** gli ordini Base
pagati nella finestra 30–45 giorni, non ancora contattati e non abbonati; dopo la
marcatura l'ordine esce dalla selezione e `updated_at` resta invariato. Tutti i 4
moduli compilano.

## Controlli admin (tab "Clienti")

Nella scheda di ogni cliente (modale) è presente la sezione **"Upsell Base → Completo"**:
- **Stato**: "non ancora inviata" / "inviata il <data>" / "cliente già abbonato — non pertinente".
- **Invio manuale**: pulsante *"Invia email upsell"* (o *"Invia di nuovo"*). Bypassa la
  finestra 30–45 giorni ed è utile per test o invii mirati. Bloccato se il cliente ha
  già un abbonamento attivo. Dopo l'invio il profilo si ricarica e mostra la data.

La sezione compare solo per i clienti con almeno un ordine Base pagato.

**Endpoint / file:**
- `POST /api/admin/customers/{email}/send-upsell` (main.py) — invia + marca l'upsell
  dell'ordine Base pagato più recente; rifiuta se il cliente è abbonato attivo o non ha
  ordini Base pagati.
- `GET /api/admin/customers/{email}` ora include `base_upsell` (stato) e
  `base_upsell_sent_at` su ogni ordine.
- `storage.get_customer_orders` espone la colonna `base_upsell_sent_at`.
- `frontend/admin.html` — sezione + funzioni `renderUpsellSection` / `customerSendUpsell`.

## Idee future (non implementate)
- A/B test dell'oggetto email.
- Secondo tocco a ~50 giorni per chi non ha aperto/convertito (attenzione a non
  saturare).
- Tracciare le conversioni attribuite via `utm_campaign=base_upsell` in dashboard.
