# NutriScienza — Backend

Backend FastAPI che orchestra: questionario → Stripe Checkout → generazione del piano via Claude → PDF → email.

## Architettura

```
Cliente compila questionario
         ↓
POST /api/intake
         ↓
Calcolo target nutrizionali (Mifflin-St Jeor + PAL + deficit)
         ↓
Salvataggio ordine (SQLite)
         ↓
Stripe Checkout Session  ──→  cliente paga su pagina hosted Stripe
                                         ↓
                            POST /api/stripe/webhook (firma verificata)
                                         ↓
                              checkout.session.completed
                                         ↓
                            BackgroundTask:
                              1. Claude → MealPlan strutturato (JSON)
                              2. ReportLab → PDF brandizzato
                              3. Resend → email con PDF in allegato
```

## Setup locale

### 1. Prerequisiti
- Python 3.11+
- Account Stripe (modalità test va benissimo)
- Account Anthropic con accesso API
- Account Resend con dominio verificato (o usa il dominio sandbox)

### 2. Installazione

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# poi edita .env con le tue chiavi
```

### 3. Stripe — crea i tre prodotti

Vai su [Stripe Dashboard → Products](https://dashboard.stripe.com/test/products) e crea:

| Prodotto         | Prezzo  | Modalità                      | Variabile env             |
|------------------|---------|-------------------------------|---------------------------|
| Piano Base       | €19,00  | Una tantum (one-time)         | `STRIPE_PRICE_BASE`       |
| Piano Completo   | €29,00  | Mensile ricorrente            | `STRIPE_PRICE_COMPLETO`   |
| Piano Coach      | €99,00  | Mensile ricorrente            | `STRIPE_PRICE_COACH`      |

Copia il `price_id` (formato `price_...`) di ognuno nel file `.env`.

### 4. Stripe — configura il webhook

In dev locale, usa la Stripe CLI per inoltrare eventi sul tuo PC:

```bash
# Installa: https://stripe.com/docs/stripe-cli
stripe login
stripe listen --forward-to localhost:8000/api/stripe/webhook
```

La CLI stamperà un `whsec_...` — incollalo in `.env` come `STRIPE_WEBHOOK_SECRET`.

In produzione, registra l'endpoint su [Stripe Webhooks](https://dashboard.stripe.com/test/webhooks):
- URL: `https://api.nutriscienza.org/api/stripe/webhook`
- Eventi da ascoltare:
  - `checkout.session.completed`
  - `checkout.session.expired`
  - `checkout.session.async_payment_failed`
  - `invoice.payment_succeeded`
  - `invoice.payment_failed`
  - `customer.subscription.deleted`

### 5. Resend — verifica il dominio

1. Crea account su [resend.com](https://resend.com)
2. Aggiungi `nutriscienza.org` come sending domain
3. Aggiungi i record DNS richiesti (SPF, DKIM)
4. Genera un'API key e mettila in `RESEND_API_KEY`

Se non hai ancora il dominio, puoi testare con `onboarding@resend.dev` come `FROM_EMAIL` (le email arriveranno solo a te, l'owner dell'account).

### 6. Anthropic

1. Crea account su [console.anthropic.com](https://console.anthropic.com)
2. Genera API key e mettila in `ANTHROPIC_API_KEY`
3. (Opzionale) cambia `ANTHROPIC_MODEL` se vuoi un modello più economico per i test (`claude-haiku-4-5-20251001`)

### 7. Avvia il server

```bash
uvicorn app.main:app --reload --port 8000
```

Il server ascolta su `http://localhost:8000`. Verifica con:

```bash
curl http://localhost:8000/healthz
# → {"status":"ok","version":"0.1.0","environment":"development"}
```

## Test del flusso end-to-end

1. Avvia il backend: `uvicorn app.main:app --reload --port 8000`
2. Avvia la Stripe CLI in un altro terminale: `stripe listen --forward-to localhost:8000/api/stripe/webhook`
3. Apri `questionario.html` in locale (file://) — automatico aggancia `localhost:8000` come API base
4. Compila il questionario fino allo step 6, scegli "Piano Base", premi "Procedi al pagamento"
5. Vieni reindirizzato a Stripe Checkout — usa `4242 4242 4242 4242`, qualsiasi data futura, qualsiasi CVC
6. Il webhook riceve `checkout.session.completed`, parte la pipeline
7. Controlla i log: dovresti vedere `[ord_xxx] start pipeline → richiesta piano → costruisco PDF → invio email → pipeline completata`
8. Controlla la tua casella email (deve essere quella inserita nel form)

## Struttura

```
backend/
├── README.md
├── requirements.txt
├── .env.example
├── data/                  # creata a runtime — SQLite + PDF generati
│   ├── orders.db
│   └── pdfs/
└── app/
    ├── __init__.py
    ├── config.py           # Settings da .env
    ├── models.py           # Pydantic — IntakeRequest, NutritionTargets, MealPlan
    ├── nutrition.py        # Calcoli BMR/TDEE/macros (deterministici)
    ├── plan_generator.py   # Claude → MealPlan JSON
    ├── pdf_builder.py      # MealPlan + targets → PDF brandizzato
    ├── email_sender.py     # Resend con allegato
    ├── stripe_handlers.py  # Checkout Session + verifica webhook
    ├── storage.py          # SQLite — orders + subscribers
    ├── scheduler.py        # Cron job — rinnovo mensile Piano Completo/Coach
    └── main.py             # FastAPI app + routing
```

## Deploy in produzione

### Opzione A: Render (più semplice)

1. Push del codice su GitHub
2. Su [Render](https://render.com) → New Web Service → connetti repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Aggiungi tutte le variabili d'ambiente nel pannello
6. Aggiungi un disco persistente di 1 GB montato su `/opt/render/project/src/data` (SQLite + PDF)
7. Aggiorna `DATABASE_PATH=/opt/render/project/src/data/orders.db` nel pannello

**Cron job per Piano Completo** — Aggiungi un secondo servizio su Render:
- Tipo: **Cron Job**
- Command: `python -m app.scheduler`
- Schedule: `0 7 * * *` (ogni giorno alle 07:00 UTC)
- Stesse variabili d'ambiente del Web Service
- **Importante**: stesso disco persistente montato sullo stesso path — altrimenti il cron non vede il DB

Costo: $7/mese per il piano Starter. Sufficiente fino a centinaia di ordini al giorno.

### Opzione B: Fly.io

Crea `Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

Poi:
```bash
fly launch
fly volumes create data --size 1 --region cdg
fly deploy
```

### Considerazioni di scaling

SQLite va bene fino a ~10k ordini totali. Se prevedi crescita superiore:
- Migra a Postgres (es. Neon o Supabase) — cambia `storage.py`
- Sposta i PDF su S3/R2 invece del filesystem locale
- Sposta la pipeline di generazione in una coda (es. Redis + RQ, o Inngest) invece di `BackgroundTasks` di FastAPI — questo evita di perdere lavoro se il server riparte mentre stai generando un piano

Per ora `BackgroundTasks` è ok per MVP. Stripe ritenta il webhook fino a 3 giorni, quindi anche se il server cade durante la generazione il prossimo retry farà ripartire il processo (perché lo stato sull'ordine resta `paid` se la generazione non è completata).

## Sicurezza

- ✅ Stripe webhook firma sempre verificata (no replay attack)
- ✅ PCI scope minimo: i dati della carta non passano mai dal nostro server (Stripe Checkout hosted)
- ✅ Secrets solo in `.env`, mai in git
- ⚠️ Aggiungere rate limiting su `/api/intake` prima del lancio pubblico (es. `slowapi`)
- ⚠️ Aggiungere CAPTCHA invisibile (Cloudflare Turnstile) su `/api/intake` per evitare spam-on-LLM
- ⚠️ Aggiungere log retention e GDPR delete endpoint (gli intake contengono dati sanitari leggeri)

## Costi unitari per ordine (stima)

| Voce                      | Costo                        |
|---------------------------|------------------------------|
| Stripe (Piano Base €19)   | €0,55 + 1.4% = ~€0,82        |
| Claude Sonnet (~6k token) | ~€0,07                       |
| Resend (email + allegato) | ~€0,001                      |
| Hosting / DB / storage    | ~€0,01 ammortizzato          |
| **Totale**                | **~€0,90 per ordine Base**   |

Margine lordo Piano Base: ~95%. Margine Piano Completo (€29/mese): >95%, e a ricavi ricorrenti.

## TODO post-MVP

- [ ] Pagina `grazie.html` che fa polling su `/api/orders/{id}` per mostrare stato in tempo reale
- [ ] Webhook `customer.subscription.deleted` → email di conferma cancellazione all'utente
- [ ] Form di check-in mensile (aggiorna peso attuale → `storage.update_subscriber_intake`) così il cron usa i dati freschi
- [ ] Rate limiting su `/api/intake` (slowapi) + Cloudflare Turnstile CAPTCHA
- [ ] Sentry per monitorare pipeline failures in produzione
- [ ] Test su `nutrition.py` (calcoli deterministici — facile da coprire con pytest)
- [ ] Pannello admin per vedere ordini, riprocessare i falliti, statistiche
- [ ] Test (pytest) — almeno per `nutrition.py` (logica pura, facile da testare)
- [ ] Sentry per monitoring errori
