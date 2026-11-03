# wp-export — WordPress → Markdown per opticienId

Haalt alle content en beelden uit de WordPress-sites van de opticiens en zet die om
naar Markdown **zonder HTML-tags**, in één map per `opticienId`. Optioneel wordt
alles meteen naar een Google Drive-map geüpload.

Doel: neutrale export als basis voor een migratie of een test met een ander framework
(Astro, Next.js, Hugo, een headless CMS, ...).

## Wat wordt er opgehaald?

| Bron | Hoe |
|---|---|
| Pagina's, berichten en **alle custom post types** (bv. producten, merken, team) | WordPress REST API (`/wp-json/wp/v2/...`), automatisch gedetecteerd via `/wp/v2/types` |
| Categorieën, tags, eigen taxonomieën | Als namen in de frontmatter |
| ACF-velden (als ze in de API zichtbaar zijn) | In de frontmatter, HTML eruit gehaald |
| Yoast SEO-titel en -beschrijving | In de frontmatter (`seo:`) |
| **Volledige mediabibliotheek** + alle beelden in de content | Als URL (niet gedownload), opgelijst in `images.md` met alt-tekst/titel/caption |
| Achtergrondbeelden van page builders (Elementor, Divi, ...) | Uit inline `style="background-image:..."` |
| Lazy-loaded beelden (`data-src`, `srcset`, `<picture>`) | Grootste/originele versie |

Beelden worden **niet gedownload**: de Markdown verwijst rechtstreeks naar de URL op de
site. Staat een verkleinde WordPress-versie (`foto-300x200.jpg`) in de mediabibliotheek,
dan wordt naar het origineel (`foto.jpg`) verwezen (uitzetten met `--no-originals`).

**Fallback:** staat de REST API dicht (security-plugin, ...), dan crawlt het script de
sitemap (`wp-sitemap.xml`, Yoast/RankMath `sitemap_index.xml`, robots.txt) en haalt het de
hoofdcontent uit elke pagina (header, footer, menu en sidebar worden weggelaten).

## Resultaat

```
export/
├── 1234/                     ← opticienId
│   ├── index.md              ← site-info + overzicht van alle documenten
│   ├── sitemap.md            ← sitestructuur: menu's (+ diagram), paginaboom, berichten per jaar
│   ├── sitemap.json          ← idem, machineleesbaar
│   ├── manifest.json         ← machineleesbaar overzicht (voor migratiescripts)
│   ├── pages/
│   │   ├── home.md
│   │   └── contact.md
│   ├── posts/
│   │   └── nieuwe-collectie.md
│   ├── <custom-post-type>/
│   ├── images.md             ← tabel: beeld-URL, alt, titel, caption
│   └── huisstijl/
│       ├── huisstijl.md      ← leesbaar overzicht: kleuren, fonts, logo
│       ├── huisstijl.json    ← alle details (voor generatie)
│       └── tokens.css        ← CSS-variabelen + @font-face/@import
├── 5678/
└── _errors.json              ← enkel als er sites mislukt zijn
```

Elk `.md`-bestand heeft YAML-frontmatter:

```markdown
---
title: Over ons
slug: over-ons
type: page
id: 42
status: publish
url: https://www.optiek-voorbeeld.be/over-ons/
path: /over-ons/
date: '2024-05-01T10:00:00'
modified: '2025-02-11T09:12:44'
parent_id: 12
featured_image: https://www.optiek-voorbeeld.be/wp-content/uploads/2024/05/winkel.jpg
seo:
  title: Over ons | Optiek Voorbeeld
  description: Al 30 jaar uw opticien in ...
---

# Over ons

Wij zijn **opticien** & audioloog.

![Onze winkel](https://www.optiek-voorbeeld.be/wp-content/uploads/2024/05/winkel.jpg)
```

Beelden en links zijn absolute URL's naar de originele site.
De `path` is handig om bij een migratie dezelfde URL's (of redirects) op te zetten.

## Installatie

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp sites.example.csv sites.csv   # vul aan met opticienId + URL
```

Of met uv: `uv venv && uv pip install -r requirements.txt`.

## sites.csv

Komma, puntkomma of tab als scheidingsteken (Excel-export werkt dus ook):

```csv
opticien_id,url,username,app_password
1234,https://www.optiek-voorbeeld.be,,
5678,https://www.optiek-ander.be,exportuser,abcd efgh ijkl mnop qrst uvwx
```

`username` + `app_password` zijn optioneel. Met een **WordPress Application Password**
(Gebruikers → Profiel → Application Passwords) worden ook concepten, privépagina's en
de ruwe velden mee geëxporteerd. Zonder enkel wat publiek is.

## Gebruik

```bash
# Alle sites lokaal exporteren
python wp_export.py --sites sites.csv --out ./export

# Exporteren + uploaden naar Google Drive
python wp_export.py --sites sites.csv --out ./export --upload

# Eén site testen
python wp_export.py --site https://www.optiek-voorbeeld.be --opticien-id 1234 --out ./export

# Enkel bepaalde opticiens (opnieuw) doen
python wp_export.py --sites sites.csv --only 1234 5678 --upload
```

| Optie | Uitleg |
|---|---|
| `--workers 3` | Aantal sites tegelijk |
| `--all-media` | Ook URL's van PDF's, documenten en video uit de mediabibliotheek oplijsten |
| `--no-originals` | Verwijs naar de beeld-URL zoals in de content, niet naar het origineel |
| `--max-pages 2000` | Limiet voor de sitemap-fallback |
| `--no-huisstijl` | Huisstijl niet ophalen |
| `--huisstijl-only` | Enkel de huisstijl ophalen (snel, geen content) |
| `-v` | Debug-logging met volledige foutmeldingen |

Bij uploaden worden enkel nieuwe of gewijzigde bestanden naar Drive gestuurd.
Exitcode is `1` als minstens één site mislukt is (zie `_errors.json`).

## Google Drive instellen

Zet in `.env` de ID van de hoofdmap (`DRIVE_PARENT_FOLDER_ID`, het laatste deel van de
map-URL `https://drive.google.com/drive/folders/<ID>`). Per opticien wordt daaronder een
map `<opticienId>` aangemaakt (of hergebruikt als die al bestaat).

Kies één manier van inloggen:

**Standaard: rclone met je eigen Google-account (geen Cloud Console nodig)**
1. `sudo dnf install rclone` (of zie https://rclone.org/install/)
2. `rclone config create gdrive drive scope=drive` → log in de browser in met je account.
3. `python wp_export.py --sites sites.csv --upload`

Elke site wordt na de export meteen naar `<DRIVE_PARENT_FOLDER_ID>/<opticienId>` geüpload
en daarna lokaal verwijderd, zodat er hoogstens `--workers` sites tegelijk op schijf staan.
Gebruik `--keep-local` om de lokale kopie te bewaren. Mislukt de upload, dan blijft de
lokale map staan en komt de fout in `_errors.json`. `--rclone-remote` / `RCLONE_REMOTE`
kiest een andere remote.

Wil je toch de Drive API rechtstreeks gebruiken, zet dan `--uploader api` (of
`UPLOADER=api`) en kies A of B:

**A. Service account (aanbevolen voor automatisatie)**
1. Google Cloud Console → project → *Google Drive API* inschakelen.
2. *IAM → Service accounts* → account aanmaken → sleutel (JSON) downloaden.
3. Voeg het e-mailadres van de service account toe als **Inhoudsbeheerder** van een
   **Gedeelde Drive** (Shared Drive).
   ⚠️ Service accounts hebben geen eigen opslagruimte: uploaden naar een gewone
   "Mijn Drive"-map faalt met `storageQuotaExceeded`. Gebruik dus een Gedeelde Drive.
4. `.env`: `GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json`

**B. Inloggen als jezelf (OAuth)**
1. Google Cloud Console → *Google Drive API* inschakelen → *Credentials* →
   OAuth client ID van het type **Desktop app** → JSON downloaden.
2. `.env`: `GOOGLE_OAUTH_CLIENT_FILE=client_secret.json`
3. De eerste run opent een browser om in te loggen; het token komt in `token.json`.

**C. Zonder API**
Met Google Drive for Desktop of `rclone` kan je `--out` gewoon naar de gesynchroniseerde
Drive-map laten wijzen, of achteraf `rclone copy ./export gdrive:WordPress-export`.

## Sitemap

`sitemap.md` toont de structuur van de site:

- **Navigatie**: hoofd- en footermenu's zoals bezoekers ze zien (uit de HTML van de
  homepage), met submenu's en links naar de geëxporteerde bestanden, plus een
  Mermaid-diagram van het hoofdmenu (rendert in VS Code, GitHub, Obsidian).
- **Paginastructuur**: boom van alle pagina's volgens het URL-pad; ★ = staat in een menu.
- **Pagina's niet in een menu**: handig om verborgen/vergeten pagina's te vinden.
- **Berichten en andere post types** per jaar.
- **In sitemap.xml maar niet geëxporteerd**: per sectie geteld (bv. webshopproducten).

## Huisstijl

Per site komt in `huisstijl/` de huisstijl uit **Themeco Pro**. De Pro-instellingen zitten
niet publiek in de REST API, maar Pro zet ze als gegenereerde CSS in elke pagina
(`<style id="cs-inline-css">`). Het script leest die van de homepage:

- **Kleuren uit de Theme Options**: tekst (`body`), titels (`h1`), achtergrond, links,
  knoppen (`.x-btn`), navbar, accentkleur.
- **Palet**: alle kleuren in de CSS (Theme Options + Cornerstone header/footer/secties),
  gesorteerd op gebruik. CSS van WordPress zelf (blokpresets) telt niet mee.
- **Primary**: knopkleur → accentkleur → linkkleur (de eerste die geen grijs/wit/zwart is),
  anders de meest gebruikte niet-neutrale kleur. **Secondary**: de volgende meest gebruikte
  kleur met een duidelijk andere tint (kleuren van social-media-iconen uitgesloten).
  In `primary_basis` / `secondary_basis` staat telkens waarop de keuze gebaseerd is.
- **Fonts**: familie, gewicht, grootte voor tekst, titels en navigatie, met de bron:
  Google Fonts (incl. CSS-URL) of eigen fontbestanden (`@font-face`, met bestands-URL's).
- **Logo, favicon en apple-touch-icon** (URL's).

Primary/secondary zijn een afleiding: controleer ze even in `huisstijl.md`.

## Beperkingen

- Content die enkel via JavaScript wordt ingeladen (sommige sliders, externe widgets)
  zit niet in de HTML en dus ook niet in de export.
- Formulieren (Contact Form 7, Gravity Forms, ...) worden weggelaten; enkel de tekst blijft.
- Menu's, widgets en thema-instellingen worden niet geëxporteerd (geen deel van de REST-content).
- WooCommerce-producten komen mee als custom post type (`product`) zolang ze publiek in
  de REST API staan; prijzen/voorraad zitten in de WooCommerce-API en vallen erbuiten.
