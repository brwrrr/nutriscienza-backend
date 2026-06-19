# Fix — Rinnovi mensili non consegnati (scheduler)

**Data:** 2026-06-19
**Caso scatenante:** Maria Caterina (`mariacaterina86.mcs@gmail.com`, piano *completo*) addebitata
al rinnovo del 14/06/2026 ma **nessuna email** e **nessun nuovo piano**. I log del cron Render:
`Subscriber in scadenza oggi: 0` il 14/06 e il 15/06.

---

## 1. Causa radice

Il rinnovo/consegna del piano era guidato **solo** dal cron `python -m app.scheduler`, eseguito su
Render come **Cron Job separato**.

Il database è **SQLite su file locale** (`config.database_path = ./data/orders.db`). Su Render un
Cron Job gira in un **container separato con disco separato** dal Web Service, e i dischi persistenti
**non sono condivisibili tra servizi**. Risultato:

- Il cron esegue `storage.init_db()` e crea/legge un DB **vuoto, tutto suo**.
- `get_subscribers_due_for_refresh()` ritorna sempre **0** → `Subscriber in scadenza oggi: 0`, ogni
  giorno, per sempre.
- Stripe, intanto, addebita il rinnovo in autonomia → il cliente paga ma **non riceve nulla**.

Il vecchio handler `invoice.payment_succeeded` confermava solo lo stato e loggava *"cron genererà il
piano"* — ma il cron non può vedere quel subscriber. La consegna non era mai agganciata all'evento di
pagamento (l'unica fonte di verità del "ha pagato il mese N").

> Nota: la query del cron usava già `next_plan_due_at <= now` (self-healing, corretta). Il problema
> non era la logica di matching ma **il DB che il cron non poteva leggere**.

---

## 2. Cosa è stato cambiato (codice)

Principio: **agganciare la consegna all'evento di pagamento Stripe, in-process (stesso DB del Web
Service)**, con uno sweep giornaliero come backstop e un pulsante manuale come rete di sicurezza.

### `app/stripe_handlers.py` → invariato (solo lettura)

### `app/main.py`
- `from . import affiliate, scheduler, storage` (aggiunto `scheduler`).
- **Webhook `invoice.payment_succeeded` (rinnovi)** ora è il percorso PRIMARIO: su ogni rinnovo
  (`billing_reason != subscription_create`) accoda in background
  `scheduler.refresh_by_stripe_subscription(sub_id, invoice_id)` → genera piano + PDF + email subito.
  Idempotente sull'`invoice_id` (i retry del webhook non ri-generano).
- **Nuovo** `POST /api/admin/subscribers/{sub_id}/send-plan` (auth admin): invio manuale forzato
  (`force=True`) del piano del mese successivo. Alimenta il pulsante in admin.
- **Nuovo** `POST /api/admin/run-refresh` (auth admin): esegue lo sweep giornaliero **in-process**.
  Da chiamare via HTTP dal Cron Job Render (stesso DB), al posto di `python -m app.scheduler`.

### `app/scheduler.py`
- **Nuovo** `refresh_subscriber(sub_id, *, force, invoice_id, reason)`: punto d'ingresso unico
  (webhook, sweep, pulsante manuale). Gestisce errori (notifica admin, non solleva) e idempotenza:
  - `skip` se subscriber inesistente / non `active` (salvo `force`);
  - `skip` se lo stesso `invoice_id` è già stato fulfillato (idempotenza forte);
  - `skip` se un piano è già stato inviato negli ultimi **25 giorni** (`RECENT_SEND_GUARD_DAYS`) —
    evita doppio invio quando webhook e sweep si sovrappongono. `force=True` lo bypassa.
- **Nuovo** `refresh_by_stripe_subscription(stripe_sub_id, invoice_id)`: mappa la subscription Stripe
  al subscriber interno.
- `run()`: ora riusa `refresh_subscriber` per ogni riga dovuta (conteggi success/skip/fail).
- `_refresh_one` → rinominata `_deliver_plan` (solo pipeline; il `mark_plan_sent` è ora nel chiamante,
  così l'`invoice_id` viene registrato correttamente).

### `app/storage.py`
- Migrazione additiva: `ALTER TABLE subscribers ADD COLUMN last_invoice_id TEXT` (auto in `init_db`).
- `mark_plan_sent(subscriber_id, invoice_id=None)`: registra `last_invoice_id` per l'idempotenza.
- **Nuovo** `get_subscriber_by_id(subscriber_id)`.

### `frontend/admin.html`
- Tab **Subscriber**: nuova colonna **Azioni** con pulsante **✉️ Invia piano** (solo piani
  *completo*/*coach*) → chiama `POST /api/admin/subscribers/{id}/send-plan` con conferma.

---

## 3. Flusso dopo il fix

```
Rinnovo Stripe ──► webhook invoice.payment_succeeded ──► refresh_subscriber (in-process, stesso DB)
                                                          │  genera piano + PDF + email
                                                          └─ mark_plan_sent(invoice_id)  [idempotente]

Backstop:  Cron giornaliero ──HTTP──► POST /api/admin/run-refresh ──► sweep next_plan_due_at <= now
Manuale:   Admin ──► ✉️ Invia piano ──► POST .../send-plan (force=True)
```

Tre percorsi, **una sola** funzione idempotente: nessun doppio invio, nessun rinnovo perso anche se
un webhook va perso.

---

## 4. Azioni richieste su Render / Stripe (per il redeploy)

1. **Web Service — DB su disco persistente.** Assicurarsi che il Web Service abbia il disco
   persistente montato (es. `/var/data`) e che `DATABASE_PATH=/var/data/orders.db` (env). Il DB deve
   vivere lì, non su disco effimero.
2. **Stripe webhook.** Nel Dashboard Stripe, l'endpoint webhook deve includere l'evento
   `invoice.payment_succeeded` (oltre a `checkout.session.completed`,
   `customer.subscription.deleted`, `invoice.payment_failed`, `charge.refunded`).
3. **Cron Job Render — da processo a HTTP.** Cambiare il comando del Cron Job da
   `python -m app.scheduler` a una chiamata HTTP al Web Service (così legge lo stesso DB):
   ```bash
   curl -fsS -X POST https://nutriscienza-api.onrender.com/api/admin/run-refresh \
     -H "Authorization: Bearer $ADMIN_API_KEY"
   ```
   (impostare `ADMIN_API_KEY` come env del Cron Job). Schedule invariato: `0 7 * * *`.
   In alternativa il cron può essere disattivato: il webhook è ora il percorso primario.
4. **Deploy** backend + frontend. La migrazione `last_invoice_id` parte da sola all'avvio (`init_db`).

> ⚠️ Esistono copie duplicate del frontend in `outputs/` (root) e in `outputs/frontend/`. La copia
> **deployata** è il repo git `outputs/frontend/` — è quella modificata qui. Allineare/ignorare la
> copia root.

---

## 5. Recupero del cliente impattato (Maria Caterina)

È stata addebitata il 14/06 ma non ha ricevuto il mese 2. Dopo il deploy:
- Aprire **Admin → Subscriber → ✉️ Invia piano** sulla sua riga (consegna il mese 2 con `force=True`),
  **oppure** chiamare una volta `POST /api/admin/run-refresh` (lo sweep la prende: `active`,
  `next_plan_due_at` 14/06 già scaduto).
- Consigliato: una breve email di scuse per ridurre rischio churn/chargeback.

---

## 6. Verifica consigliata post-deploy

- `GET /healthz` → ok.
- Admin → Subscriber: la riga di Maria mostra il pulsante; cliccare e verificare email + PDF + che
  `Mese` passi a 2 e `Prossimo piano` avanzi di ~30 giorni.
- Stripe (test mode): forzare un rinnovo e verificare nei log
  `Rinnovo confermato ... — genero il piano` + consegna.
- Test idempotenza: re-inviare lo stesso webhook → log `invoice ... già processato — skip`.
